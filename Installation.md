### **Installation Guide: LibreQoS MikroTik PPP and Active Hotspot User Sync**

This guide will walk you through the installation and setup of the **LibreQoS MikroTik PPP and Active Hotspot User Sync** script. The script synchronizes MikroTik PPP secrets (PPPoE users) and active hotspot users with a LibreQoS-compatible CSV file (`ShapedDevices.csv`). It continuously monitors the MikroTik router for changes and ensures the CSV file remains up to date. The script runs as a background service using `systemd`.

---

### **Prerequisites**
Before proceeding, ensure the following:
1. **Python 3** is installed on your system.
2. **`routeros_api` Python library** is installed.
3. **MikroTik Router** is accessible and configured with PPP secrets and hotspot users.
4. **LibreQoS** is set up and requires the `ShapedDevices.csv` file.
5. **A `network.json` file** is manually configured for proper bandwidth management.

---

### **Step 1: Install Python and Required Libraries**
1. **Install Python 3** (if not already installed):
   ```bash
   sudo apt update
   sudo apt install python3 python3-pip
   ```

2. **Install the `routeros_api` library**:
   ```bash
   sudo apt install pipx
   pipx install routeros_api
   ```

---

### **Step 2: Clone and Run the Installation Script**
1. **Clone this repository**:
   ```bash
   git clone https://github.com/Kintoyyy/MikroTik-LibreQos-Integration
   ```

2. **Navigate to the directory**:
   ```bash
   cd MikroTik-LibreQos-Integration
   ```

3. **Make the script executable**:
   ```bash
   chmod +x install_updatecsv.sh
   ```

4. **Run the script**:
   ```bash
   sudo ./install_updatecsv.sh
   ```

---

### **Step 3: Verify the Installation**
1. **Check the Python script**:
   - The script should be located at `/opt/libreqos/src/updatecsv.py`.
   - Verify its contents:
     ```bash
     cat /opt/libreqos/src/updatecsv.py
     ```

2. **Check the systemd service file**:
   - The service file should be located at `/etc/systemd/system/updatecsv.service`.
   - Verify its contents:
     ```bash
     cat /etc/systemd/system/updatecsv.service
     ```

3. **Check the service status**:
   - Use the following command to check if the service is running:
     ```bash
     sudo systemctl status updatecsv.service
     ```
   - The output should show `active (running)`.

4. **Check the config file**:
   - The config file should be located at `/opt/libreqos/src/config.json`.
   - Verify its contents:
     ```bash
     cat /opt/libreqos/src/config.json
     ```

---


### **Step 4: Configure the Script**
If you need to customize the script settings (e.g., change the MikroTik router IP or credentials), follow these steps:

1. **Edit the `config.json` file**:
   ```bash
   sudo nano /opt/libreqos/src/config.json
   ```

2. **Modify the configuration**:
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

3. **Save and exit** the file.

4. **Restart the service**:
   ```bash
   sudo systemctl restart updatecsv.service
   ```

5. **Check the logs if running successfully**:
   ```bash
   journalctl -u updatecsv.service --no-pager --since "1 hour ago"
   ```

---

### **Step 5: Test the Script**
1. **Modify a PPP secret or active hotspot user on your MikroTik router**.
2. **Check the `ShapedDevices.csv` file**:
   ```bash
   cat /opt/libreqos/src/ShapedDevices.csv
   ```
3. **Check the logs**:
   ```bash
   journalctl -u updatecsv.service
   ```

---

### **Step 6: Enable the Service to Start on Boot**
Ensure the service starts automatically:
```bash
sudo systemctl enable updatecsv.service
```

---

### **Configuration File (config.json) Details**

The `config.json` file allows you to configure one or multiple MikroTik routers and their services:

#### **Global Configuration Options**

| Parameter | Description | Required |
|-----------|-------------|----------|
| `flat_network` | single network without hierarchical parent nodes | Yes |
| `no_parent` | devices from all routers will not have a parent node | Yes |
| `preserve_network_config` | allows dynamic updates to nodes | Yes |

#### **Router Connection Settings**

| Parameter | Description | Required |
|-----------|-------------|----------|
| `name` | Friendly name for the router | Yes |
| `address` | IP address of the router | Yes |
| `port` | API port number (default: 8728) | Yes |
| `username` | API username | Yes |
| `password` | API password | Yes |

#### **DHCP Configuration**

| Parameter | Description | Required |
|-----------|-------------|----------|
| `enabled` | Enable DHCP client tracking | Yes |
| `download_limit_mbps` | Default download speed for DHCP clients | If enabled |
| `upload_limit_mbps` | Default upload speed for DHCP clients | If enabled |
| `dhcp_server` | Array of DHCP server names | If enabled |

#### **Hotspot Configuration**

| Parameter | Description | Required |
|-----------|-------------|----------|
| `enabled` | Enable hotspot user tracking | Yes |
| `include_mac` | Include MAC addresses for hotspot users | If enabled |
| `download_limit_mbps` | Default download speed for hotspot users | If enabled |
| `upload_limit_mbps` | Default upload speed for hotspot users | If enabled |

#### **PPPoE Configuration**

| Parameter | Description | Required |
|-----------|-------------|----------|
| `enabled` | Enable PPPoE user tracking | Yes |
| `per_plan_node` | Create separate parent nodes for each PPP profile | If enabled |

---

### **Troubleshooting**
1. **Service not running**:
   - Check the logs:
     ```bash
     journalctl -u updatecsv.service
     ```
   - Ensure `routeros_api` is installed:
     ```bash
     pip3 show routeros_api
     ```

2. **CSV file not updating**:
   - Verify router details in `config.json`.
   - Ensure PPP secrets and active hotspot users exist on the MikroTik router.
   - Check if the JSON file is valid:
     ```bash
     jq . /opt/libreqos/src/config.json
     ```

3. **Rate limit values incorrect**:
   - Modify the values in the `config.json` file.
   - Restart the service after changes.

4. **Permission issues**:
   ```bash
   sudo chown -R root:root /opt/libreqos/src
   sudo chmod -R 755 /opt/libreqos/src
   sudo chmod 640 /opt/libreqos/src/config.json
   ```

---

### **Uninstallation**
1. **Stop and disable the service**:
   ```bash
   sudo systemctl stop updatecsv.service
   sudo systemctl disable updatecsv.service
   ```

2. **Remove the service file**:
   ```bash
   sudo rm /etc/systemd/system/updatecsv.service
   ```

3. **Remove the script and directory**:
   ```bash
   sudo rm -rf /opt/libreqos/src/updatecsv.py
   sudo rm -rf /opt/libreqos/src/config.json
   ```

4. **Reload systemd**:
   ```bash
   sudo systemctl daemon-reload
   ```

---

This guide ensures smooth installation and operation of the **LibreQoS MikroTik PPP and Active Hotspot User Sync** script. If you encounter issues, refer to the troubleshooting section or check the logs for detailed error messages.

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

### **Thank You!**
Your support motivates further development and improvements to this script. If you have any feedback, feature requests, or issues, feel free to open an issue on the project's GitHub repository. Together, we can make network management easier and more efficient for everyone.

Happy networking! 🚀