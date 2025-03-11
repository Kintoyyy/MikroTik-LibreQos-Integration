### **Installation Guide: LibreQoS MikroTik PPP and Active Hotspot User Sync**

This guide will walk you through the installation and setup of the **LibreQoS MikroTik PPP and Active Hotspot User Sync** script. The script synchronizes MikroTik PPP secrets (PPPoE users) and active hotspot users with a LibreQoS-compatible CSV file (`ShapedDevices.csv`). It continuously monitors the MikroTik router for changes and ensures the CSV file remains up to date. The script runs as a background service using `systemd`.

---

### **Prerequisites**
Before proceeding, ensure the following:
1. **Python 3** is installed on your system.
2. **`routeros_api` Python library** is installed.
3. **MikroTik Router** is accessible and configured with PPP secrets and hotspot users.
4. **LibreQoS** is set up and requires the `ShapedDevices.csv` file.
5. **A `routers.csv` file** is used to store MikroTik credentials for easy management.
6. **A `network.json` file** is manually configured for proper bandwidth management.

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

---

### **Step 4: Configure `network.json` Manually**
The `network.json` file must be manually configured to include the routers listed in `routers.csv`. Each router should have child nodes prefixed with `PPP-` and `HS-`.

1. **Edit the `network.json` file**:
   ```bash
   sudo nano /opt/libreqos/src/network.json
   ```

2. **Ensure it follows this structure:**
   ```json
   {
       "MikroTik-XYZ": {
           "downloadBandwidthMbps": 2000,
           "uploadBandwidthMbps": 2000,
           "type": "site",
           "children": {
               "PPP-MikroTik-XYZ": {
                   "downloadBandwidthMbps": 1000,
                   "uploadBandwidthMbps": 1000,
                   "type": "site",
                   "children": {}
               },
               "HS-MikroTik-XYZ": {
                   "downloadBandwidthMbps": 1000,
                   "uploadBandwidthMbps": 1000,
                   "type": "site",
                   "children": {}
               }
           }
       }
   }
   ```

3. **Save and exit** the file.

4. **Restart the LibreQoS service**:
   ```bash
   sudo systemctl restart libreqos
   ```

---

### **Step 5: Configure the Script**
If you need to customize the script (e.g., change the MikroTik router IP or credentials), follow these steps:

1. **Edit the `routers.csv` file**:
   ```bash
   sudo nano /opt/libreqos/src/routers.csv
   ```
   - Format:
     ```csv
      Router Name / ID,IP,API Username,API Password,API Port
      MikroTik-XYZ,192.168.88.1,api,password,8728
     ```

2. **Restart the service**:
   ```bash
   sudo systemctl restart updatecsv.service
   ```
3. **Check the logs if running successfully**:
   ```bash
   journalctl -u updatecsv.service --no-pager --since "1 hour ago"
   ```

---

### **Step 6: Test the Script**
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

### **Step 7: Enable the Service to Start on Boot**
Ensure the service starts automatically:
```bash
sudo systemctl enable updatecsv.service
```

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
   - Verify router details in `routers.csv`.
   - Ensure PPP secrets and active hotspot users exist on the MikroTik router.

3. **Rate limit values incorrect**:
   - Modify the logic in `updatecsv.py` if needed.

4. **Permission issues**:
   ```bash
   sudo chown -R root:root /opt/libreqos/src
   sudo chmod -R 755 /opt/libreqos/src
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
   sudo rm -rf /opt/libreqos/src
   ```

4. **Reload systemd**:
   ```bash
   sudo systemctl daemon-reload
   ```

---

This guide ensures smooth installation and operation of the **LibreQoS MikroTik PPP and Active Hotspot User Sync** script. If you encounter issues, refer to the troubleshooting section or check the logs for detailed error messages.

