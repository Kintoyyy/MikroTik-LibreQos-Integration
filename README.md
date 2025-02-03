### **Script Description: LibreQoS MikroTik PPP Secret Sync**

This script automates the synchronization of MikroTik PPP secrets (e.g., PPPoE users) with a LibreQoS-compatible CSV file (`ShapedDevices.csv`). It continuously monitors the MikroTik router for changes to PPP secrets, such as additions, updates, or deletions, and updates the CSV file accordingly. The script also calculates rate limits (download/upload speeds) based on the assigned PPP profile and ensures the CSV file is always up-to-date.

The script is designed to run as a background service using `systemd`, ensuring it starts automatically on boot and restarts in case of failures.

---

### **Key Features**
1. **Automatic Synchronization**:
   - Regularly checks for changes in MikroTik PPP secrets.
   - Updates the `ShapedDevices.csv` file with new, modified, or deleted entries.

2. **Rate Limit Calculation**:
   - Extracts rate limits from MikroTik PPP profiles.
   - Calculates minimum rates as 50% of the maximum rates for both download and upload.

3. **Logging**:
   - Logs all actions (additions, updates, deletions) for easy monitoring and debugging.

4. **Systemd Integration**:
   - Runs as a background service with automatic restarts.
   - Ensures the script starts on system boot.

5. **Customizable Configuration**:
   - Easily configure the MikroTik router IP, credentials, and CSV file path.

---

### **Use Case**
This script is ideal for network administrators using **LibreQoS** for traffic shaping and **MikroTik** routers for PPPoE management. It ensures that the `ShapedDevices.csv` file used by LibreQoS is always synchronized with the latest PPP secrets and rate limits from the MikroTik router.

---

### **How It Works**
1. **Connects to MikroTik Router**:
   - Uses the `routeros_api` Python library to connect to the MikroTik router and fetch PPP secrets.

2. **Processes PPP Secrets**:
   - Compares the current PPP secrets with the existing CSV data.
   - Adds new entries, updates modified entries, and removes deleted entries.

3. **Writes to CSV**:
   - Updates the `ShapedDevices.csv` file with the latest data in the required format for LibreQoS.

4. **Runs Continuously**:
   - The script runs in an infinite loop, checking for changes every 10 seconds.

---

### **Prerequisites**
- Python 3 installed on the system.
- `routeros_api` Python library installed (`pip install routeros_api`).
- MikroTik router with PPP secrets configured.
- LibreQoS setup requiring the `ShapedDevices.csv` file.

---

### **Installation and Usage**
1. **Run the Installation Script**:
   - Execute the provided `.sh` script to install the Python script and systemd service.

2. **Start the Service**:
   - The script will automatically start the service and enable it to run on boot.

3. **Monitor the Service**:
   - Use `systemctl status updatecsv.service` to check the status and logs.

---

### **Example Output**
The script will generate a `ShapedDevices.csv` file with the following columns:
- `Circuit ID`, `Circuit Name`, `Device ID`, `Device Name`, `Parent Node`, `MAC`, `IPv4`, `IPv6`, `Download Min Mbps`, `Upload Min Mbps`, `Download Max Mbps`, `Upload Max Mbps`, `Comment`

Example CSV Entry:
```
Circuit ID,Circuit Name,Device ID,Device Name,Parent Node,MAC,IPv4,IPv6,Download Min Mbps,Upload Min Mbps,Download Max Mbps,Upload Max Mbps,Comment
550e8400-e29b-41d4-a716-446655440000,PPPoE_User1,550e8400-e29b-41d4-a716-446655440001,PPPoE_User1,,,192.168.1.10,,50,25,100,50,Test User
```

---

### **Why Use This Script?**
- **Automation**: Eliminates manual updates to the `ShapedDevices.csv` file.
- **Accuracy**: Ensures rate limits and PPP secret data are always in sync with the MikroTik router.
- **Efficiency**: Saves time and reduces errors in managing LibreQoS traffic shaping.
