### **LibreQoS MikroTik PPP and Active Hotspot User Sync**

This script automates the synchronization of MikroTik PPP secrets (e.g., PPPoE users) and active hotspot users with a LibreQoS-compatible CSV file (`ShapedDevices.csv`). It continuously monitors the MikroTik router for changes to PPP secrets and active hotspot users, such as additions, updates, or deletions, and updates the CSV file accordingly. The script also calculates rate limits (download/upload speeds) based on the assigned PPP profile and ensures the CSV file is always up-to-date.

The script is designed to run as a background service using `systemd`, ensuring it starts automatically on boot and restarts in case of failures.

---

### **Key Features**
1. **Automatic Synchronization**:
   - Regularly checks for changes in MikroTik PPP secrets and active hotspot users.
   - Updates the `ShapedDevices.csv` file with new, modified, or deleted entries.
   - Updates occur every 10 minutes.

2. **Rate Limit Calculation**:
   - Extracts rate limits from MikroTik PPP profiles.
   - Computes minimum rates as 50% of maximum values and 115% for the maximum.

3. **Logging**:
   - Logs all actions (additions, updates, deletions) for easy monitoring and debugging.

4. **Systemd Integration**:
   - Runs as a background service with automatic restarts.
   - Ensures the script starts on system boot.

5. **Customizable Configuration**:
   - Configure routers via the `config.json` file with detailed settings for DHCP, hotspot, and PPPoE.
   - Simple JSON structure allows for easy management of router credentials and service settings.

---

### **Use Case**
This script is ideal for network administrators using **LibreQoS** for traffic shaping and **MikroTik** routers for managing PPPoE and hotspot users. It ensures that the `ShapedDevices.csv` file used by LibreQoS is always synchronized with the latest PPP secrets and active hotspot users from the MikroTik router.

---

### **How It Works**
1. **Connects to MikroTik Router**:
   - Uses the `routeros_api` Python library to connect to the MikroTik router and fetch PPP secrets and active hotspot users.
   - Reads connection parameters from the `config.json` file.

2. **Processes PPP Secrets and Active Hotspot Users**:
   - Compares the current PPP secrets and active hotspot users with the existing CSV data.
   - Adds new entries, updates modified entries, and removes deleted entries.

3. **Writes to CSV**:
   - Updates the `ShapedDevices.csv` file with the latest data in the required format for LibreQoS.

4. **Runs Continuously**:
   - The script runs in an infinite loop, checking for changes every 10 minutes.

---

### **Prerequisites**
- Python 3 installed on the system.
- `routeros_api` Python library installed (`pip install routeros_api`).
- MikroTik router with configured PPP secrets and active hotspot users.
- LibreQoS setup requiring the `ShapedDevices.csv` file.

---

### **Installation and Usage**
1. **Run the Installation Script**:
   - Execute the provided `.sh` script to install the Python script and systemd service.
   - The script automatically creates a `config.json` file if it doesn't exist.

2. **Configure the Settings**:
   - Edit the `config.json` file to match your MikroTik router details and feature settings.

3. **Start the Service**:
   - The script will automatically start the service and enable it to run on boot.

4. **Monitor the Service**:
   - Use `systemctl status updatecsv.service` to check the status and logs.

---

### **Configuration File (config.json) Details**

The `config.json` file is the central configuration point for the script. It allows you to configure one or multiple MikroTik routers and their associated services (DHCP, Hotspot, PPPoE). Below is a detailed explanation of each configuration option:

#### **Basic Structure**

```json
{
    "routers": [
        {
            "name": "Router Name",
            "address": "Router IP Address",
            "port": Port Number,
            "username": "API Username",
            "password": "API Password",
            "dhcp": { /* DHCP Configuration */ },
            "hotspot": { /* Hotspot Configuration */ },
            "pppoe": { /* PPPoE Configuration */ }
        }
    ]
}
```

#### **Router Connection Settings**

| Parameter | Description | Example |
|-----------|-------------|---------|
| `name` | Friendly name for the router | `"Mikrotik 1"` |
| `address` | IP address of the router | `"192.168.88.1"` |
| `port` | API port number (default: 8728) | `8728` |
| `username` | API username | `"admin"` |
| `password` | API password | `"password"` |

#### **DHCP Configuration**

```json
"dhcp": {
    "enabled": true,
    "download_limit_mbps": 1000,
    "upload_limit_mbps": 1000,
    "dhcp_server": [
        "dhcp1",
        "dhcp2"
    ]
}
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| `enabled` | Enable DHCP client tracking | `true` or `false` |
| `download_limit_mbps` | Default download speed limit for DHCP clients (Mbps) | `1000` |
| `upload_limit_mbps` | Default upload speed limit for DHCP clients (Mbps) | `1000` |
| `dhcp_server` | Array of DHCP server names to monitor | `["dhcp1", "dhcp2"]` |

#### **Hotspot Configuration**

```json
"hotspot": {
    "enabled": true,
    "include_mac": true,
    "download_limit_mbps": 10,
    "upload_limit_mbps": 10
}
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| `enabled` | Enable hotspot user tracking | `true` or `false` |
| `include_mac` | Include MAC addresses for hotspot users | `true` or `false` |
| `download_limit_mbps` | Default download speed limit for hotspot users (Mbps) | `10` |
| `upload_limit_mbps` | Default upload speed limit for hotspot users (Mbps) | `10` |

#### **PPPoE Configuration**

```json
"pppoe": {
    "enabled": true,
    "per_plan_node": true
}
```

| Parameter | Description | Example |
|-----------|-------------|---------|
| `enabled` | Enable PPPoE user tracking | `true` or `false` |
| `per_plan_node` | Create separate parent nodes for each PPP profile | `true` or `false` |

#### **Multiple Router Example**

```json
{
    "routers": [
        {
            "name": "Mikrotik AC",
            "address": "10.0.0.2",
            "port": 8728,
            "username": "LibreQos",
            "password": "ABC11233",
            "dhcp": {
                "enabled": false,
                "download_limit_mbps": 1000,
                "upload_limit_mbps": 1000,
                "dhcp_server": []
            },
            "hotspot": {
                "enabled": false,
                "include_mac": true,
                "download_limit_mbps": 10,
                "upload_limit_mbps": 10
            },
            "pppoe": {
                "enabled": true,
                "per_plan_node": true
            }
        },
        {
            "name": "Mikrotik AC",
            "address": "10.0.0.3",
            "port": 8728,
            "username": "LibreQos",
            "password": "1234ABC",
            "dhcp": {
                "enabled": true,
                "download_limit_mbps": 100,
                "upload_limit_mbps": 100,
                "dhcp_server": [
                    "DHCP_LAN"
                ]
            },
            "hotspot": {
                "enabled": true,
                "include_mac": true,
                "download_limit_mbps": 10,
                "upload_limit_mbps": 10
            },
            "pppoe": {
                "enabled": false,
                "per_plan_node": true
            }
        }
    ]
}
```

---

### **MikroTik Configuration**
To ensure proper access, create a dedicated user group and user on the MikroTik router:

```shell
/user group add name=API_READ policy="read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,!write,!test,!winbox,!password,!web,!sniff,!romon"
/user add name="libreQos_API" group=API_READ password="<Strong Password>" address="<LibreQos IP Address>" disabled=no;
```

This ensures the API user has the necessary permissions while restricting unnecessary access.

---

### **Example Output**
The script will generate a `ShapedDevices.csv` file with the following columns:
- `Circuit ID`, `Circuit Name`, `Device ID`, `Device Name`, `Parent Node`, `MAC`, `IPv4`, `IPv6`, `Download Min Mbps`, `Upload Min Mbps`, `Download Max Mbps`, `Upload Max Mbps`, `Comment`

Example CSV Entry:
```
Circuit ID,Circuit Name,Device ID,Device Name,Parent Node,MAC,IPv4,IPv6,Download Min Mbps,Upload Min Mbps,Download Max Mbps,Upload Max Mbps,Comment
49A05TNK,VC1234,H6ZO7WEL,VC1234,HS-ROUTER,5C:1C:B9:CD:4B:D1,10.0.0.230,,3,3,8,8,Hotspot
HI11R8ZV,USER1,DX29J8P8,USER1,PPP-ROUTER,,10.120.00.254,,5,5,11,111,PPPoE
```

---

### **Why Use This Script?**
- **Automation**: Eliminates manual updates to the `ShapedDevices.csv` file.
- **Accuracy**: Ensures rate limits and PPP secret data are always in sync with the MikroTik router.
- **Efficiency**: Saves time and reduces errors in managing LibreQoS traffic shaping.
- **Flexibility**: Configure multiple routers and their features through the centralized `config.json` file.
- **Granular Control**: Fine-tune settings for each service type (DHCP, Hotspot, PPPoE) on a per-router basis.

---