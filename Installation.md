### **Installation Guide: LibreQoS MikroTik PPP Secret Sync**

This guide will walk you through the installation and setup of the **LibreQoS MikroTik PPP Secret Sync** script. The script synchronizes MikroTik PPP secrets with a LibreQoS-compatible CSV file (`ShapedDevices.csv`) and runs as a background service using `systemd`.

---

### **Prerequisites**
Before proceeding, ensure the following:
1. **Python 3** is installed on your system.
2. **`routeros_api` Python library** is installed.
3. **MikroTik Router** is accessible and configured with PPP secrets.
4. **LibreQoS** is set up and requires the `ShapedDevices.csv` file.

---

### **Step 1: Install Python and Required Libraries**
1. **Install Python 3** (if not already installed):
   ```bash
   sudo apt update
   sudo apt install python3 python3-pip
   ```

2. **Install the `routeros_api` library**:
   ```bash
   pip3 install routeros_api
   ```

---

### **Step 2: Download and Run the Installation Script**
1. **Download the installation script**:
   - Save the script provided in the previous response to a file, e.g., `install_updatecsv.sh`.

2. **Make the script executable**:
   ```bash
   chmod +x install_updatecsv.sh
   ```

3. **Run the script**:
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

### **Step 4: Configure the Script (Optional)**
If you need to customize the script (e.g., change the MikroTik router IP or credentials), follow these steps:
1. **Edit the Python script**:
   ```bash
   sudo nano /opt/libreqos/src/updatecsv.py
   ```
   - Update the following variables as needed:
     ```python
     ROUTER_IP = '172.2.0.237'  # Replace with your MikroTik router IP
     USERNAME = 'demo'          # Replace with your MikroTik username
     PASSWORD = 'demo123'       # Replace with your MikroTik password
     CSV_FILE = 'ShapedDevices.csv'  # Replace with your desired CSV file path
     ```

2. **Restart the service**:
   ```bash
   sudo systemctl restart updatecsv.service
   ```

---

### **Step 5: Test the Script**
1. **Add or modify a PPP secret on your MikroTik router**.
2. **Check the `ShapedDevices.csv` file**:
   - The file should be updated with the new or modified PPP secret.
   - Example location: `/opt/libreqos/src/ShapedDevices.csv`.
   - View the file:
     ```bash
     cat /opt/libreqos/src/ShapedDevices.csv
     ```

3. **Check the logs**:
   - Use the following command to view the script logs:
     ```bash
     journalctl -u updatecsv.service
     ```

---

### **Step 6: Enable the Service to Start on Boot**
The installation script should have already enabled the service to start on boot. To confirm:
```bash
sudo systemctl is-enabled updatecsv.service
```
If not enabled, run:
```bash
sudo systemctl enable updatecsv.service
```

---

### **Troubleshooting**
1. **Service not running**:
   - Check the logs for errors:
     ```bash
     journalctl -u updatecsv.service
     ```
   - Ensure the `routeros_api` library is installed:
     ```bash
     pip3 show routeros_api
     ```

2. **CSV file not updating**:
   - Verify the MikroTik router IP, username, and password in the script.
   - Ensure the PPP secrets exist on the MikroTik router.

3. **Permission issues**:
   - Ensure the script and directories have the correct permissions:
     ```bash
     sudo chown -R root:root /opt/libreqos/src
     sudo chmod -R 755 /opt/libreqos/src
     ```

---

### **Uninstallation**
To remove the script and service:
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

This installation guide ensures a smooth setup and operation of the **LibreQoS MikroTik PPP Secret Sync** script. If you encounter any issues, refer to the troubleshooting section or consult the logs for detailed error messages.
