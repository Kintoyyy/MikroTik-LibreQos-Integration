# MikroTik Rate Setup Guide

This guide explains how to configure bandwidth rates on your MikroTik router so the integration can automatically pick them up and apply them to LibreQoS.

Rates are driven by **Firewall Address List entries** whose list name encodes the rate in `download/upload` format (e.g., `50M/50M`). For PPPoE and Hotspot, the recommended way to populate these entries is through the `address-list` field on PPP/Hotspot user profiles — MikroTik handles adding and removing IPs automatically on connect/disconnect.

---

## How Rate Lookup Works

### PPPoE and Hotspot

```
Active session
      │
      │  IP address
      ▼
Firewall Address List
      │
      │  Matched entry → list name = rate string (e.g. "50M/50M")
      ▼
LibreQoS CSV entry with resolved rate
```

When a PPPoE or Hotspot session becomes active, the script looks up the session's IP in `/ip/firewall/address-list`. If it finds a matching entry, the **list name** is treated as the rate. If no match is found, the `default_download_limit` / `default_upload_limit` from `config.json` is used.

### IPoE / DHCP

IPoE and DHCP work the same way. The rate is read from the `address-list` field on the static lease record. If the lease is dynamic or has no `address-list` set, the device falls back to the default speed from `config.json`.

---

## Rate String Format

| Format | Download | Upload |
|--------|----------|--------|
| `10M/10M` | 10 Mbps | 10 Mbps |
| `50M/25M` | 50 Mbps | 25 Mbps |
| `100M/50M` | 100 Mbps | 50 Mbps |
| `1G/1G` | 1000 Mbps | 1000 Mbps |
| `512k/256k` | 0.51 Mbps | 0.26 Mbps |

Units: `k` = kbps, `M` = Mbps (default if no unit), `G` = Gbps. Case-insensitive.

---

## PPPoE Rate Setup

MikroTik PPP profiles have a built-in `address-list` field. When a PPPoE user connects using a profile that has `address-list` set, MikroTik **automatically** adds the session IP to that address list — no scripts needed.

### Step 1 — Create PPP profiles with address-list

```shell
/ppp profile
add name=10Mbps  address-list=10M/10M
add name=20Mbps  address-list=20M/20M
add name=50Mbps  address-list=50M/50M
add name=100Mbps address-list=100M/50M
add name=1Gbps   address-list=1G/1G
```

### Step 2 — Assign the profile to PPPoE users

```shell
/ppp secret
set [find name="john"]  profile=50Mbps
set [find name="jane"]  profile=100Mbps
set [find name="bob"]   profile=20Mbps
```

> When a user connects, their IP is automatically added to the matching address list (e.g., `50M/50M`). When they disconnect, the entry is removed. No manual address list management required.

### Step 3 — Verify in WinBox

1. Open **IP → Firewall → Address Lists**
2. While a user is connected, confirm their IP appears under the correct list name:

   | List | Address |
   |------|---------|
   | `50M/50M` | `10.10.1.5` |
   | `100M/50M` | `10.10.1.6` |
   | `20M/20M` | `10.10.1.7` |

3. The list name must exactly match the pattern `{number}{unit}/{number}{unit}` — no spaces, no extra characters.

---

## Hotspot Rate Setup

Same approach — Hotspot user profiles have an `address-list` field that automatically places the session IP into the specified list on login.

### Step 1 — Create Hotspot user profiles with address-list

```shell
/ip hotspot user profile
add name=5Mbps  address-list=5M/5M
add name=10Mbps address-list=10M/10M
add name=20Mbps address-list=20M/20M
add name=50Mbps address-list=50M/50M
```

### Step 2 — Assign the profile to Hotspot users

```shell
/ip hotspot user
set [find name="user1"] profile=10Mbps
set [find name="user2"] profile=50Mbps
```

> MikroTik adds the user's IP to the address list on login and removes it on logout automatically.

---

## IPoE / DHCP Rate Setup

IPoE and DHCP work the same way. Leases **must be static** (not dynamic) to have the `address-list` field set. Dynamic leases do not carry this field and will fall back to the default speed from `config.json`.

### Step 1 — Make the lease static and set address-list

**WinBox:**
1. Go to **IP → DHCP Server → Leases**
2. Find the lease and click **Make Static** (if it's dynamic)
3. Double-click the static lease
4. In the **Address List** field, enter the rate string: e.g., `50M/50M`
5. Click OK

**CLI:**
```shell
/ip dhcp-server lease
add mac-address=AA:BB:CC:DD:EE:FF address=192.168.1.100 address-list=50M/50M comment="desktop-john"
add mac-address=11:22:33:44:55:66 address=192.168.1.101 address-list=100M/50M comment="desktop-jane"
```

> Dynamic leases have no `address-list` field — they will use the default speed defined in `config.json`.

---

## RADIUS / User Manager Rate Setup

For any RADIUS manager (FreeRADIUS, User Manager, Radiusdesk, etc.), all you need is the `Mikrotik-Address-List` reply attribute set to the rate string. MikroTik will automatically add the session IP to that address list on connect.

> Use `Mikrotik-Address-List`, **not** `Mikrotik-Rate-Limit`. The integration reads rates from the firewall address list name, not from the rate-limit string.

**Any RADIUS manager** — just return this attribute in your Access-Accept:
```
Mikrotik-Address-List = 50M/50M
```

### MikroTik User Manager

### Step 1 — Create user groups with the attribute

```shell
/user-manager user group
add name=10Mbps  attributes=Mikrotik-Address-List:10M/10M
add name=20Mbps  attributes=Mikrotik-Address-List:20M/20M
add name=50Mbps  attributes=Mikrotik-Address-List:50M/50M
add name=100Mbps attributes=Mikrotik-Address-List:100M/50M
add name=1Gbps   attributes=Mikrotik-Address-List:1G/1G
```

### Step 2 — Assign users to a group

```shell
/user-manager user
set [find name="john"] group=50Mbps
set [find name="jane"] group=100Mbps
```

> When the user connects, MikroTik adds their IP to the address list defined in the group's attribute (e.g., `50M/50M`). The integration picks it up from there.

### Manual Firewall Address List (any setup)

You can also add IPs directly to the firewall address list without any profile or RADIUS — useful for static devices or quick testing:

```shell
/ip firewall address-list
add list=50M/50M  address=10.10.1.5 comment="john"
add list=100M/50M address=10.10.1.6 comment="jane"
add list=10M/10M  address=10.10.1.7 comment="bob"
```

> Entries added manually are permanent and are not removed on disconnect. Suitable for static IPs; for dynamic IPs use profiles or RADIUS attributes instead.

---

## Full CLI Example

Below is a complete setup for a small network with three PPPoE customers on different plans:

```shell
# Create firewall address list entries per customer/rate
/ip firewall address-list
add list=10M/10M  address=10.0.0.10 comment="basic-plan-user1"
add list=10M/10M  address=10.0.0.11 comment="basic-plan-user2"
add list=50M/50M  address=10.0.0.12 comment="standard-plan-user1"
add list=100M/50M address=10.0.0.13 comment="premium-plan-user1"
add list=1G/1G    address=10.0.0.14 comment="business-plan-user1"
```

The integration will map these to LibreQoS shaped devices automatically with the following applied rates:

| List name | Applied max DL | Applied max UL | Min DL | Min UL |
|-----------|---------------|---------------|--------|--------|
| `10M/10M` | 12 Mbps | 12 Mbps | 6 Mbps | 6 Mbps |
| `50M/50M` | 58 Mbps | 58 Mbps | 29 Mbps | 29 Mbps |
| `100M/50M` | 115 Mbps | 58 Mbps | 58 Mbps | 29 Mbps |
| `1G/1G` | 1150 Mbps | 1150 Mbps | 575 Mbps | 575 Mbps |

> Max = parsed rate × 1.15 | Min = max × 0.50

---

## Tips

- Keep list names consistent and lowercase for readability (e.g., `50m/50m` works too)
- Use comments on address list entries to identify customers — the script preserves comments in CSV output
- PPPoE and Hotspot: use the `address-list` field on profiles (or `Mikrotik-Address-List` via RADIUS) — MikroTik manages entries automatically on connect/disconnect
- DHCP: leases must be **static** to have a rate; dynamic leases fall back to the default speed
- Manual firewall address list entries work for any source and are useful for static IPs or quick testing
