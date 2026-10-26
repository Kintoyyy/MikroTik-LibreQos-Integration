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
| `"auto_cpu"` | Keeps the site → AP → router hierarchy from [`topology`](#topology-auto-cpu) **and** balances it over the CPU cores: one `CPU<n>` node per LibreQoS queue, whole subtrees packed onto them, and sites or routers too heavy for one core split automatically. Edit it visually in the **Builder** tab. See [Auto CPU](#auto-cpu-auto_cpu). |

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
Multi-site network on a multi-core LibreQoS box, and you want both
site visibility and every core busy?
  → auto_cpu (it promotes heavy nodes and balances the cores itself)
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
| `true` | `cpu`: the system CPU count. `auto_cpu`: the queue count LibreQoS itself will use (see below) |
| `false` | Skip `network.json` entirely (no queue nodes created) |
| `2`, `4`, `8`, ... | Use exactly this many CPU queue nodes |

```json
"queues": 4
```

> **Tip:** Set `queues` to match the number of physical CPU cores available to LibreQoS for optimal bin-packing. With `auto_cpu`, leave it `true`: more CPU nodes than LibreQoS has queues puts two of them on one core.

---

### `topology` (Auto CPU)

The site hierarchy used by the `auto_cpu` strategy. The **Builder** tab edits it with drag and drop; you rarely need to write it by hand.

```json
"topology": {
    "version": 1,
    "nodes": [
        { "id": "n_north", "name": "Region North", "type": "site",
          "download_mbps": 0, "upload_mbps": 0, "virtual": true, "promote": false, "pin_cpu": null,
          "routers": [],
          "children": [
              { "id": "n_tower1", "name": "Tower N1", "type": "ap",
                "download_mbps": 4000, "upload_mbps": 2000, "virtual": false, "promote": false, "pin_cpu": null,
                "routers": ["BRAS-N1"], "children": [] }
          ] }
    ],
    "router_options": { "BRAS-Core": { "pin_cpu": null, "download_mbps": 0, "upload_mbps": 0, "shard": true } }
}
```

| Field | Meaning |
|-------|---------|
| `id` | Stable id (letters, digits, `_ . : -`, up to 48). Keeps a renamed node on its CPU on LibreQoS 2.x |
| `name` | `network.json` node name. Unique, not equal to a router name, and not `CPU<n>` / `Generated_PN_<n>` |
| `type` | `site` or `ap` |
| `download_mbps` / `upload_mbps` | Node limit. `0` = sum of its subscribers' max rates × `parent_bandwidth_multiplier` |
| `virtual` | Group only: LibreQoS does not shape it (its limit is ignored) |
| `promote` | Always split this node's children across CPUs (LibreQoS `promote_to_root`) |
| `pin_cpu` | Put this node (whole) on `CPU<n>`; `null` = automatic. Only for nodes that are placed whole |
| `routers` | Router names (as in `bras`) whose subscribers hang off this node. A router is in at most one node |
| `children` | Child nodes, at most 6 levels deep (CPU node + 6 + router = HTB's 8 levels) |
| `router_options` | Per router: `pin_cpu`, a `download_mbps`/`upload_mbps` limit, and `shard: false` to never split it |

Routers not listed in any node are top-level items. Without a `topology` key, `auto_cpu` builds one from each router's `site` field. Saving in the Builder also writes each router's `site` (its top-level node), so `ap_site`/`full` group routers the same way.

### Auto CPU (`auto_cpu`)

LibreQoS shapes each top-level `network.json` node, with everything under it, on **one** CPU core. Without an Insight license it assigns top-level nodes to cores round-robin, and `use_binpacking` falls back to round-robin too. A single big site therefore saturates one core while the others idle. Every sync cycle, `auto_cpu` does what the LibreQoS scale-planning guide recommends, automatically:

1. **Counts the queues LibreQoS will use**: the tx queues of the shaping interfaces (or `[queues] override_available_queues`), capped at the CPU cores (E-cores excluded when `exclude_efficiency_cores = true`), halved on a stick. It writes exactly that many top-level nodes, `CPU0` … `CPU<n-1>`, so each one lands on its own core.
2. **Promotes heavy nodes**: a site or AP carrying more than one core's fair share of all plans (`auto_cpu_split_ratio`), or more than `cpu_capacity_mbps` at the estimated peak, is dissolved; its children become separate items that can sit on different cores.
3. **Packs whole subtrees onto the CPU nodes**, heaviest first, and keeps them there: an item moves only when the busiest CPU carries more than `cpu_rebalance_ratio` × the lightest, and at most `auto_cpu_move_budget` items move per cycle. Every move rebuilds those circuits' queues, so the plan stays stable.
4. **Spreads routers too heavy for one core over the CPUs**: their subscribers fill the room the whole subtrees leave, until every CPU carries the same load. A router uses its own CPUs first and then the emptiest ones, so it occupies as few CPUs as it can, with one shard node per CPU it uses: `Router`, `Router #2`, …. A subscriber stays on its CPU unless that CPU is more than 5% over its share, so subscribers only move when the load really shifts, and names stay the same from one cycle to the next. A split router returns to a single node once its load drops 20% below the limit.

The estimated peak is the plan sum × (`[queues] downlink_bandwidth_mbps` ÷ all plans). Without a link rate in `lqos.conf` only the balancing applies.

**lqos.conf advice for `auto_cpu`** (`updatecsv` and the Builder warn about each):

- `[queues] use_binpacking = false`. With Insight, LibreQoS would re-pack the already balanced CPU nodes.
- `[stormguard] targets` should list `CPU0` … `CPU<n-1>` only.
- `[treeguard.links] top_level_auto_virtualize = false`. Virtualizing an idle CPU node would move its subtrees onto other cores.

`promote_to_root` is ignored with `auto_cpu`; it promotes nodes itself.

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
| `monitor_interfaces` | string[] | No | Interfaces graphed on the Pollers page (at most 8), e.g. `["sfp-sfpplus1", "ether2"]`. Empty or missing: the router's running ethernet ports. Pick them in the GUI with **Pick from router**. |
| `snmp` | object | No | `{"enabled": true, "community": "lqsync", "port": 161}`: poll health, PPP sessions and interface counters over SNMP v2c instead of the API (see [Router monitoring](#router-monitoring)). |
| `webhook_token` | string | No | Token of this router's PPP webhook (see [Instant PPP Updates](#instant-ppp-updates-webhook-optional)). Set from the GUI. |

### Router monitoring

The GUI polls every BRAS once a minute and keeps the history in RRD files (`rrd/` next to `config.json`), graphed on the **Pollers** page:

| Graph | What | How it is read |
|-------|------|----------------|
| Router *name* | CPU, memory, temperature, voltage, API latency | `/system/resource`, `/system/health` |
| Router *name* PPP sessions | Active PPP sessions | `/ppp/active` with `count-only`, so a large BRAS does not send every session |
| Router *name* *interface* | rx / tx Mbps, with rx / tx errors and drops per minute marked as ticks | The interface's `rx-byte`, `tx-byte`, `rx-error`, `tx-error`, `rx-drop` and `tx-drop` counters, stored as rates |

The Pollers router cards and the Overview router tiles show the latest values: the PPP count, and each interface's current rx/tx rate with any errors or drops in the last minute. "rx" and "tx" are from the router's side of that interface: on the uplink, rx is your subscribers' download.

#### Over SNMP instead of the API

Turn on **Poll over SNMP** in a router's Monitoring section (or set `"snmp": {"enabled": true, ...}`) to read all of the above over SNMP v2c, so the once-a-minute poll no longer opens an API session. LQ-Sync has its own small SNMP client: nothing extra to install. On the router:

```
/snmp set enabled=yes
/snmp community add name=lqsync addresses=<LQ-Sync address>/32 read-access=yes write-access=no
```

Use that community name in LQ-Sync and press **Test SNMP**. Prefer a community limited to LQ-Sync's address over the default `public`, and allow UDP 161 from LQ-Sync if a firewall sits in between.

| Value | SNMP source |
|-------|-------------|
| Identity, board, version | `sysName`, `sysDescr`, MIKROTIK-MIB `mtxrLicVersion` |
| CPU, memory, uptime | HOST-RESOURCES-MIB (`hrProcessorLoad`, `hrStorage` main memory, `hrSystemUptime`) |
| Temperature, voltage | MIKROTIK-MIB `mtxrHlTemperature` / `mtxrHlVoltage` (RouterOS 6), or the `mtxrGaugeTable` (RouterOS 7) |
| Active PPP sessions | Each PPP session is a dynamic `<pppoe-…>` interface, so sessions = `ifNumber` minus the router's own interfaces. Those are counted from an `ifName` walk repeated every hour, or sooner when interfaces change |
| Interface counters | IF-MIB `ifHCInOctets` / `ifHCOutOctets` (32-bit counters when missing), `ifIn/OutErrors`, `ifIn/OutDiscards` |

With SNMP on, the API is still used for the subscriber scans (sessions, leases and address lists have no SNMP equivalent), for the NAT check (every 15 minutes instead of every minute), for **Pick from router**, and for the PPP webhook installer. The Pollers card shows how each router was polled. If you add or remove interfaces on the router, the PPP count can be off by that many until the next hourly walk.

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

Device code format: `DHCP-{MAC}` (MAC without colons). Host-names are chosen by the client and are not unique, so the host-name goes into the comment instead. Existing `DHCP-{hostname}` rows are re-keyed automatically and keep their circuit IDs. Disabled leases and leases without an address are skipped.

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

Device code format: the entry's `comment` field if set (and not already used by another entry in the same scan), otherwise `ADDR-{IP}`.

---

## Rate String Format

Rates are expressed as `{download}/{upload}` with an optional unit suffix:

| Format | Meaning |
|--------|---------|
| `50M/50M` | 50 Mbps down / 50 Mbps up |
| `100M/50M` | 100 Mbps down / 50 Mbps up |
| `1G/1G` | 1000 Mbps down / 1000 Mbps up |
| `512k/256k` | 0.51 Mbps down / 0.26 Mbps up |

The script applies these multipliers automatically (defaults follow LibreQoS's recommendation, and both are configurable in `settings.json` → `rates` or in **App Settings**):
- **Max rate** = parsed value × `max_*_rate_percentage` (default 1.15), at least 2 Mbps
- **Min rate** = max rate × `min_*_rate_percentage` (default 0), at least 1 Mbps and never above max

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

Static devices keep their rate limits, IPs, and other fields across scans, and always win an IP conflict with a scanned device. Their parent node follows the topology strategy.

---

## Duplicate Router Names

If two routers in the `routers` array share the same `name`, the script automatically appends an incrementing suffix to prevent collisions:

```
"Mikrotik 1"   → "Mikrotik 1"   (first occurrence)
"Mikrotik 1"   → "Mikrotik 1 2" (second occurrence)
```

This suffix also affects parent node names in `ap_only`/`ap_site`/`full` strategies, so prefer unique router names.

---

## Instant PPP Updates (webhook, optional)

Without it, a new PPPoE subscriber is shaped at the next poll (`scanner.scan_interval`, 10 minutes by default) and a disconnected one lingers until then. With it, each BRAS calls LQ-Sync from its PPP profiles' `on-up` / `on-down` scripts the moment a session starts or ends, and LQ-Sync adds or removes that subscriber within seconds. The regular poll keeps running and corrects anything a missed call left behind.

**Set up** on the **Routers** page, card *Instant PPP updates*:

1. Turn on **Accept webhook calls**, enter the address the routers reach LQ-Sync at (e.g. `http://10.0.0.5:5000`), and save.
2. For each BRAS, **Set up** → **Generate token**. Each router has its own token; LQ-Sync accepts a call only with a known token.
3. Install it on the router, either:
   - **Install over API**: LQ-Sync installs it directly. This needs an API account with write access (the polling account is read-only); it is used once and never stored. Or
   - **Paste in terminal**: copy the install script into the router's terminal (Winbox → New Terminal, or SSH).
4. The router sends a test call; it appears under **Last call**.

What gets installed on the router:

- `/system script lqsync-webhook`: POSTs `{"event", "user", "ip", "caller_id"}` to `<address>/api/webhook/mikrotik` with the token in an `X-LQSync-Token` header (`/tool fetch`, RouterOS 6.43+ or 7).
- One line at the top of every PPP profile's `on-up` and `on-down` script that calls it. The profiles' own lines are kept, and the removal script (or **Remove**) takes out only these lines and the script.

What LQ-Sync does with a call: the sync service collects calls for `webhook.debounce_seconds`, then, for each reported user, reads only that user's `/ppp active` entry and its IP's address-list entries. It adds or updates the user exactly as a full scan would (same rate lookup), or removes it if the session is gone, and runs the LibreQoS update if anything changed. A router that reports more than `webhook.full_scan_over` users at once (e.g. after a reboot) is rescanned in full instead. Only PPPoE is handled this way; hotspot and DHCP still update at the poll.

```json
"webhook": { "enabled": true, "url": "http://10.0.0.5:5000" },
"bras": [ { "name": "BRAS-1", "webhook_token": "3f9c…", ... } ]
```

> **Notes**
> - Keep `scanner.libreqos_update_command` set (`auto`). When it is empty, new subscribers wait for `lqos_scheduler`'s refresh (`queue_refresh_interval_mins`).
> - On LibreQoS 1.5+ the Bakery adds and removes circuits without a full reload. A circuit whose rate changed still triggers a full reload, so do frequent rate changes through the poll.
> - The calls are plain HTTP on the GUI port: keep them on your management network. The token only lets a router queue a lookup of its own sessions; LQ-Sync always reads the real state from the router.

---

## WAN Assignment (optional)

WAN assignment is **off by default**. `wan_service` is installed but not enabled, and core routers are ignored until you turn it on.

```json
"wan_assignment": { "enabled": true, "include_hotspot": false, "include_dhcp": false, "interval": 300 }
```

Turning **Enable WAN Assignment** on or off in the GUI and saving enables or disables `wan_service` (including at boot). Turning it off leaves the `libreqos-managed` address-list entries on the core routers; run **Purge** in Troubleshooting to remove them.

New devices are balanced across WANs by capacity. Existing devices move only when the busiest WAN's utilization is more than `rebalance_imbalance_ratio` (default 1.5) times the least busy WAN's, and then as few as possible move, since each move breaks that subscriber's NAT sessions.

---

## Tuning (`settings.json`)

Every tunable value lives in `settings.json` and can be edited in **App Settings**. Restart the services after changing it. Missing keys use these defaults:

| Key | Default | Meaning |
|-----|---------|---------|
| `scanner.scan_interval` | 600 | Seconds between router scans |
| `scanner.connect_retries` / `connect_retry_delay` | 3 / 5 | Connection attempts per router, and seconds between them. An unreachable router keeps its devices |
| `scanner.libreqos_update_command` | `/usr/bin/sudo /opt/libreqos/src/LibreQoS.py --updateonly` | Run when `ShapedDevices.csv` or `network.json` changes. Empty: nothing runs, and `lqos_scheduler` applies the files on its next refresh |
| `scanner.libreqos_update_timeout` | 300 | Seconds before a hung apply is stopped (it is retried next cycle) |
| `rates.min_*_rate_percentage` | 0.0 | Min rate as a fraction of max (0 → 1 Mbps) |
| `rates.max_*_rate_percentage` | 1.15 | Max rate as a multiple of the plan rate |
| `topology.parent_bandwidth_multiplier` | 1.1 | `network.json` node size = sum of its devices' max rates × this |
| `topology.skew_warn_ratio` | 2.0 | Warn when the busiest top-level/CPU node carries this many times the lightest one's load |
| `topology.sticky_cpu_assignment` | true | `cpu` strategy: devices stay on their CPU node (fewer queue rebuilds). New devices go to the least loaded node |
| `topology.cpu_rebalance_ratio` | 1.5 | Sticky mode: move devices (`cpu`) or whole sites/routers (`auto_cpu`) only when the busiest CPU node exceeds this many times the lightest |
| `topology.node_ids` | true | Add a stable `id` (`mikrotik:cpu:CPU0`, `mikrotik:router:…`) to every `network.json` node |
| `topology.virtual_site_nodes` | false | `ap_site`/`full`: mark site nodes `virtual` (grouping only, not shaped) |
| `topology.cpu_capacity_mbps` | 0 | `auto_cpu`: download Mbps one core can shape. 0 = `lqos.conf` `generated_pn_download_mbps`, else 3000. LibreQoS's guide by single-thread score: 1000 → 1 Gbps, 1500 → 2, 2000 → 3, 3000 → 4, 4000 → 5 |
| `topology.auto_cpu_split_ratio` | 1.0 | `auto_cpu`: split a site/router carrying more than this × one core's fair share of all plans. 0 = split only above `cpu_capacity_mbps` |
| `topology.auto_cpu_move_budget` | 0 | `auto_cpu`: most sites/routers moved between CPUs per cycle. 0 = 1% of them (1–32) |
| `database.tc_u16_warn_threshold` | 60000 | Warn when one CPU queue approaches the tc class ID limit (`TC_U16_OVERFLOW`) |
| `database.mapped_circuit_warn_threshold` | 1000 | Warn when circuits exceed LibreQoS's unlicensed limit. `ShapedDevices.csv` lists manual devices first, then sources by priority, so the least important circuits are dropped first. 0 disables |
| `wan_service.rebalance_imbalance_ratio` | 1.5 | See WAN Assignment |
| `wan_service.manual_rebalance_active_cutoff` | 1200 | The GUI Rebalance button treats dynamic devices not seen for this many seconds as offline |
| `gui.login_max_attempts` / `login_lockout_seconds` | 5 / 60 | Login rate limit per client address |
| `burst.enabled` | false | Burst for all traffic (see below) |
| `burst.seconds` / `max_mb` | 4 / 256 | Burst size = plan rate × seconds, at most this many MB per subscriber |
| `burst.pause_above_pct` / `resume_below_pct` | 80 / 60 | Bursts pause above this link use and return after 30 s below the second value |
| `webhook.debounce_seconds` | 2 | PPP webhook: collect a burst of calls this long before acting (at least 1: an ended session must be gone from `/ppp active`) |
| `webhook.min_interval_seconds` | 10 | PPP webhook: webhook-triggered syncs run at most this often (each may run the LibreQoS update) |
| `webhook.full_scan_over` | 50 | PPP webhook: more users than this from one router in one batch → rescan that router in full |
| `webhook.keep_days` | 7 | PPP webhook: days of received calls kept for the GUI |
| `host_snmp.enabled` | true | Overview: read this host's CPU, memory, load, uptime and interface traffic from its own snmpd (see [Overview](#overview)); psutil fills in whatever snmpd does not answer |
| `host_snmp.address` / `port` / `community` | 127.0.0.1 / 161 / public | Where this host's snmpd listens (SNMP v2c) |

`updatecsv` also checks `/etc/lqos.conf` (Python 3.11+) and logs a warning when `[topology] compile_mode = "flat"` would ignore the parent hierarchy, or when device IPs fall outside `allow_subnets` or inside `ignore_subnets`. With `auto_cpu` it also warns about the settings listed under [Auto CPU](#auto-cpu-auto_cpu).

---

## Overview

The GUI's first tab shows the shaper live:

- **Live queues, from LibreQoS's own tc tree.** While the Overview is open, the GUI reads `tc -s class show` and
  `tc -s qdisc show` on the shaping interfaces from lqos.conf every 2 s (slower when tc takes long on a very large tree; it stops
  20 s after the page is closed). LibreQoS builds one HTB tree per CPU queue (download on `to_network`, upload on `to_internet`,
  or both on the single interface on a stick), so the root class of each tree is that core's traffic. The classes are named from
  LibreQoS's `queuingStructure.json` and `statsByCircuit.json` (looked up in the LibreQoS directory, its `state/` folders and
  `/var/lib/libreqos`). Without them each subscriber shows as its class id.
  - **CPU queues:** download / upload per queue, circuits and how many are moving traffic, cake drops per second, and the load of
    the core it runs on. A core near 100% is the first thing to look at when subscribers complain.
  - **Queue map:** every subscriber moving traffic, grouped by CPU queue and network node; size is download now, colour is the share of
    their plan in use. Click a queue or node to open it.
  - **Busiest subscribers:** the top 15 now, with share of plan (⚠ at 90% or more) and drops.
- **This host.** CPU (average and per core), memory, load, uptime and interface traffic, from this host's snmpd when
  `host_snmp.enabled` (the default) and it answers. psutil fills in anything snmpd does not show, or everything while it does not
  answer (it is asked again every 30 s). The badge on the card says which was used. CPU % comes from UCD-SNMP-MIB's raw tick
  counters, which include softirq time, where LibreQoS's shaping runs. Per-core load is HOST-RESOURCES `hrProcessorLoad`,
  which snmpd averages over a minute. snmpd refreshes some counters only every few seconds, so its values move in steps.

  Ubuntu's snmpd shows only the system group by default. Open it to this host only:

  ```shell
  sudo apt install snmpd
  # /etc/snmp/snmpd.conf: replace the agentaddress and rocommunity lines with
  #   agentaddress udp:127.0.0.1:161
  #   rocommunity public 127.0.0.1
  sudo systemctl restart snmpd
  ```

  **Settings → Host Monitoring (SNMP) → Test** shows what snmpd answers and what its view still hides.
- **Routers, subscribers, activity:** each router's last poll (CPU, RAM, PPP sessions, busiest graphed interface, SNMP or API),
  devices by source, router and plan, the last sync jobs and the PPP webhook's latest events.

---

## Burst (optional)

LibreQoS holds every subscriber to their plan from the first packet. With burst on, an idle subscriber's next transfer gets
about `burst.seconds` worth of their plan at full speed, then settles to the plan — like cable "PowerBoost". It applies to all
traffic equally: page loads, updates and speedtests.

How it works: LibreQoS gives each subscriber one HTB class (`rate` = min, `ceil` = max). The GUI raises that class's `cburst`
to `ceil × seconds` with `tc class change`, leaving rate, ceil and priority alone, and re-applies it within a minute whenever
LibreQoS rebuilds its queues. LibreQoS's own files are not modified, so it works with LibreQoS 1.x and 2.x. Its runs show as the
**htb burst** job on the Pollers page.

- **Spare capacity only.** Every 10 s the GUI measures the LibreQoS link against `[queues] downlink_bandwidth_mbps` /
  `uplink_bandwidth_mbps` in lqos.conf. Above `pause_above_pct` bursts switch off; they return after 30 s below
  `resume_below_pct`. Set those two lqos.conf values to your real internet capacity, or the link never looks busy.
- **Burst speed is not capped** — only the amount is. A subscriber can briefly go as fast as their path allows, which matters on
  shared segments LibreQoS does not model (wireless sectors, PON). Keep `seconds` small there.
- Advertise it honestly (e.g. "short transfers up to …"): it is a real property of the service, not a speedtest exception.
- If the GUI service stops, subscribers simply go back to LibreQoS's normal behaviour.

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

### Multi-site, Auto CPU

```json
{
    "strategy": "auto_cpu",
    "queues": true,
    "bras": [
        { "name": "BRAS-Core",  "address": "10.0.0.1", "username": "api", "password": "secret", "port": 8728 },
        { "name": "BRAS-North", "address": "10.1.0.1", "username": "api", "password": "secret", "port": 8728 },
        { "name": "BRAS-South", "address": "10.2.0.1", "username": "api", "password": "secret", "port": 8728 }
    ],
    "topology": {
        "version": 1,
        "nodes": [
            { "id": "n_north", "name": "North POP", "type": "site", "download_mbps": 3000, "upload_mbps": 1500,
              "virtual": false, "promote": false, "pin_cpu": null, "routers": ["BRAS-North"], "children": [] },
            { "id": "n_south", "name": "South POP", "type": "site", "download_mbps": 0, "upload_mbps": 0,
              "virtual": false, "promote": false, "pin_cpu": null, "routers": ["BRAS-South"], "children": [] }
        ],
        "router_options": {}
    }
}
```

On an 8-queue LibreQoS box this writes `CPU0` … `CPU7`. When `BRAS-Core` carries most of the subscribers, `North POP` and `South POP` each stay whole on one core with their limits enforced, and `BRAS-Core` (top level) is split into shards `BRAS-Core`, `BRAS-Core #2`, … on the other cores. For example (which CPU gets which item depends on the loads):

```
CPU0 ── North POP ── BRAS-North
CPU1 ── South POP ── BRAS-South
CPU2 ── BRAS-Core
CPU3 ── BRAS-Core #2
 …
```

---

## Preview server (development)

`preview_server.py` runs the real GUI against fake data, without MikroTik, LibreQoS or root, to try the Builder and Auto CPU:

```shell
python3 preview_server.py            # http://localhost:5050, password: admin
python3 preview_server.py --reset    # start again from the seeded demo
```

It keeps everything in `.preview/` (git-ignored): a demo `config.json` with five BRAS and two regions, about 1500 subscribers, an `lqos.conf`, and a fake sysfs with two 8-queue NICs on 8 CPUs, 2 of them E-cores, so LibreQoS would use 6 queues. Services are simulated. Restarting `updatecsv` (for example Save in the Builder) runs one sync cycle in-process and writes `network.json` and `ShapedDevices.csv` to `.preview/libreqos/`. The Overview's queues come from a simulated LibreQoS, laid out from those files as LibreQoS.py would, with its `queuingStructure.json` and tc output. Its host data comes from a fake snmpd used in place of `127.0.0.1:161`. The app update button is disabled there. Options: `--port`, `--host`, `--devices`, `--seed`, `--cpus`, `--interval` and `--churn` (subscribers leaving and joining per router per cycle).

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
