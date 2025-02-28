import csv
import json
import logging
import time
import random
import string
import routeros_api
from collections import OrderedDict
from functools import lru_cache
import os

# Configuration
ROUTERS_CSV = 'routers.csv'  # CSV file containing router details
SHAPED_DEVICES_CSV = 'ShapedDevices.csv'  # Output CSV file
NETWORK_JSON = 'network.json'  # Network configuration JSON file
FIELDNAMES = [
    'Circuit ID', 'Circuit Name', 'Device ID', 'Device Name', 'Parent Node',
    'MAC', 'IPv4', 'IPv6', 'Download Min Mbps', 'Upload Min Mbps',
    'Download Max Mbps', 'Upload Max Mbps', 'Comment'
]
SCAN_INTERVAL = 600  # Time in seconds between router scans
ERROR_RETRY_INTERVAL = 30  # Time in seconds to wait after an error
MIN_RATE_PERCENTAGE = 0.5  # Calculate min rates as this percentage of max rates
MAX_RATE_PERCENTAGE = 1.15  # Calculate max rates as this percentage of bandwidth
ID_LENGTH = 8  # Length of generated short IDs
DEFAULT_BANDWIDTH = 2000  # Default bandwidth for new routers in Mbps

# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def generate_short_id(length=ID_LENGTH):
    """Generate a short random ID using numbers and uppercase letters."""
    return ''.join(random.choices(string.digits + string.ascii_uppercase, k=length))

def read_routers_csv():
    """Read router details from the CSV file."""
    routers = []
    try:
        with open(ROUTERS_CSV, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                routers.append({
                    'name': row['Router Name / ID'],
                    'ip': row['IP'],
                    'username': row['API Username'],
                    'password': row['API Password'],
                    'port': int(row['API Port'])
                })
        logger.info(f"Successfully read {len(routers)} routers from {ROUTERS_CSV}")
        return routers
    except FileNotFoundError:
        logger.error(f"Router CSV file not found: {ROUTERS_CSV}")
        return []
    except Exception as e:
        logger.error(f"Error reading router CSV: {e}")
        return []

def read_network_json():
    """Read the network configuration from JSON file."""
    try:
        if os.path.exists(NETWORK_JSON):
            with open(NETWORK_JSON, 'r') as f:
                return json.load(f)
        else:
            logger.info(f"Network JSON file not found: {NETWORK_JSON}, will create a new one.")
            return {}
    except Exception as e:
        logger.error(f"Error reading network JSON: {e}")
        return {}

def write_network_json(data):
    """Write network configuration to JSON file."""
    try:
        with open(NETWORK_JSON, 'w') as f:
            json.dump(data, f, indent=4)
        logger.info(f"Successfully wrote network configuration to {NETWORK_JSON}")
    except Exception as e:
        logger.error(f"Error writing network JSON: {e}")

def update_network_json(routers):
    """Update network.json with any missing routers."""
    network_config = read_network_json()
    updated = False
    
    for router in routers:
        router_name = router['name']
        if router_name not in network_config:
            # Add the router and its child nodes to the network configuration
            network_config[router_name] = {
                "downloadBandwidthMbps": DEFAULT_BANDWIDTH,
                "uploadBandwidthMbps": DEFAULT_BANDWIDTH,
                "type": "site",
                "children": {
                    f"PPP-{router_name}": {
                        "downloadBandwidthMbps": DEFAULT_BANDWIDTH // 2,
                        "uploadBandwidthMbps": DEFAULT_BANDWIDTH // 2,
                        "type": "site",
                        "children": {}
                    },
                    f"HS-{router_name}": {
                        "downloadBandwidthMbps": DEFAULT_BANDWIDTH // 2,
                        "uploadBandwidthMbps": DEFAULT_BANDWIDTH // 2,
                        "type": "site",
                        "children": {}
                    }
                }
            }
            logger.info(f"Added router {router_name} to network configuration")
            updated = True
    
    if updated:
        write_network_json(network_config)
    else:
        logger.info("No new routers needed to be added to network configuration")
    
    return network_config

def connect_to_router(router):
    """Connect to a MikroTik router using the provided details."""
    try:
        connection = routeros_api.RouterOsApiPool(
            router['ip'],
            username=router['username'],
            password=router['password'],
            port=router['port'],
            plaintext_login=True
        )
        api = connection.get_api()
        logger.info(f"Successfully connected to router: {router['name']} ({router['ip']})")
        return api
    except Exception as e:
        logger.error(f"Connection error to {router['name']} ({router['ip']}): {e}")
        return None

def get_resource_data(api, resource_path):
    """Get data from a specified resource path."""
    try:
        return api.get_resource(resource_path).get()
    except Exception as e:
        logger.error(f"Failed to get data from {resource_path}: {e}")
        return []

@lru_cache(maxsize=32)
def parse_rate_limit(rate_limit):
    """Parse a rate limit string and return rx, tx values."""
    try:
        if not rate_limit or rate_limit == '0/0':
            return '0', '0'
        
        first_rate = rate_limit.split()[0]  # Takes the first "7M/7M" part
        rx, tx = first_rate.split('/')  # Split into download and upload rates
        return rx.rstrip('M'), tx.rstrip('M')  # Remove 'M' suffix
    except Exception:
        logger.warning(f"Could not parse rate limit: {rate_limit}, using defaults")
        return '0', '0'

def get_profile_rate_limits(api, profile_name, resource_path):
    """Fetch rate limits for a profile from the specified resource path."""
    try:
        profiles = api.get_resource(resource_path).get(name=profile_name)
        if not profiles:
            return '0', '0'
        
        profile = profiles[0]
        rate_limit = profile.get('rate-limit', '0/0')
        return parse_rate_limit(rate_limit)
    except Exception as e:
        logger.error(f"Failed to get profile rate limits for {profile_name}: {e}")
        return '0', '0'

def read_shaped_devices_csv():
    """Read existing shaped devices from the CSV file."""
    data = OrderedDict()
    try:
        with open(SHAPED_DEVICES_CSV, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data[row['Circuit Name']] = row
        logger.info(f"Successfully read {len(data)} entries from {SHAPED_DEVICES_CSV}")
    except FileNotFoundError:
        logger.info(f"No existing CSV file found at {SHAPED_DEVICES_CSV}, will create a new one.")
    return data

def write_shaped_devices_csv(data):
    """Write shaped devices data to the CSV file."""
    with open(SHAPED_DEVICES_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in data.values():
            writer.writerow(row)
    logger.info(f"Successfully wrote {len(data)} entries to {SHAPED_DEVICES_CSV}")

def calculate_min_rates(max_rx, max_tx):
    """Calculate minimum rates based on maximum rates."""
    # Convert rates to integers for calculation
    rx_int = int(max_rx) if max_rx.isdigit() else 0
    tx_int = int(max_tx) if max_tx.isdigit() else 0
    
    # Calculate min rates as percentage of max rates
    return str(int(rx_int * MIN_RATE_PERCENTAGE)), str(int(tx_int * MIN_RATE_PERCENTAGE))

def calculate_max_rates(max_rx, max_tx):
    """Calculate minimum rates based on maximum rates."""
    # Convert rates to integers for calculation
    rx_int = int(max_rx) if max_rx.isdigit() else 0
    tx_int = int(max_tx) if max_tx.isdigit() else 0
    
    # Calculate min rates as percentage of max rates
    return str(int(rx_int * MAX_RATE_PERCENTAGE)), str(int(tx_int * MAX_RATE_PERCENTAGE))

def create_new_entry(code, router_name, entry_type, mac='', ipv4=''):
    """Create a new device entry."""
    return {
        'Circuit ID': generate_short_id(),
        'Device ID': generate_short_id(),
        'Circuit Name': code,
        'Device Name': code,
        'MAC': mac,
        'IPv4': ipv4,
        'IPv6': '',
        'Parent Node': f"{entry_type}-{router_name}",
        'Comment': entry_type,
        'Download Max Mbps': '0',
        'Upload Max Mbps': '0',
        'Download Min Mbps': '0',
        'Upload Min Mbps': '0'
    }

def update_entry_values(entry, new_values):
    """Update an entry with new values and return if any changes were made."""
    changed = False
    for k, v in new_values.items():
        if entry.get(k) != v:
            entry[k] = v
            changed = True
    return changed

def process_pppoe_users(api, router_name, existing_data):
    """Process PPPoE users from a router."""
    current_users = set()
    updated = False
    
    # Get all PPP secrets
    secrets = {s['name']: s for s in get_resource_data(api, '/ppp/secret') if 'name' in s}
    active = {a['name']: a for a in get_resource_data(api, '/ppp/active') if 'name' in a}

    # Keep only secrets that exist in active and append the "address" from active
    secrets = {
        name: {**data, 'address': active[name]['address']} 
        for name, data in secrets.items() if name in active and 'address' in active[name]
    }
    
    for code, secret in secrets.items():
        current_users.add(code)
        
        # Get or create entry
        if code in existing_data:
            entry = existing_data[code]
        else:
            entry = create_new_entry(
                code, 
                router_name, 
                'PPP', 
                '', 
                secret.get('address', '')
            )
            logger.info(f"Created new entry for PPPoE user: {code} with IDs: {entry['Circuit ID']}/{entry['Device ID']}")
            updated = True
        
        # Get rate limits
        profile_name = secret.get('profile', 'default')
        rx, tx = get_profile_rate_limits(api, profile_name, '/ppp/profile')
        rx_max, tx_max = calculate_max_rates(rx, tx)
        rx_min, tx_min = calculate_min_rates(rx, tx)
        
        # Update values
        new_values = {
            'IPv4': secret.get('address', ''),
            'Comment': secret.get('comment', 'PPP'),
            'Download Max Mbps': rx_max,
            'Upload Max Mbps': tx_max,
            'Download Min Mbps': rx_min,
            'Upload Min Mbps': tx_min
        }
        
        if update_entry_values(entry, new_values):
            logger.info(f"Updated PPPoE user: {code}")
            updated = True
        
        existing_data[code] = entry
    
    return current_users, updated

def process_hotspot_users(api, router_name, existing_data):
    """Process Hotspot users from a router."""
    current_users = set()
    updated = False
    
    # Get active hotspot users
    hotspot_users = {u['user']: u for u in get_resource_data(api, '/ip/hotspot/active') if 'user' in u}
    
    for code, user in hotspot_users.items():
        current_users.add(code)
        
        # Get or create entry
        if code in existing_data:
            entry = existing_data[code]
        else:
            entry = create_new_entry(
                code, 
                router_name, 
                'HS', 
                user.get('mac-address', ''), 
                user.get('address', '')
            )
            logger.info(f"Created new entry for Hotspot user: {code} with IDs: {entry['Circuit ID']}/{entry['Device ID']}")
            updated = True
        
        # Get rate limits (using default profile, can be enhanced)
        rx, tx = get_profile_rate_limits(api, 'default', '/ip/hotspot/user/profile')
        rx_max, tx_max = calculate_max_rates(rx, tx)
        rx_min, tx_min = calculate_min_rates(rx, tx)
        
        # Update values
        new_values = {
            'MAC': user.get('mac-address', ''),
            'IPv4': user.get('address', ''),
            'Comment': 'Hotspot',
            'Download Max Mbps': rx_max,
            'Upload Max Mbps': tx_max,
            'Download Min Mbps': rx_min,
            'Upload Min Mbps': tx_min
        }
        
        if update_entry_values(entry, new_values):
            logger.info(f"Updated Hotspot user: {code}")
            updated = True
        
        existing_data[code] = entry
    
    return current_users, updated

def process_router(api, router_name, existing_data):
    """Process all users from a router and update the existing data."""
    if not api:
        return False
    
    all_current_users = set()
    updated = False
    
    # Process PPPoE users
    pppoe_users, pppoe_updated = process_pppoe_users(api, router_name, existing_data)
    all_current_users.update(pppoe_users)
    updated = updated or pppoe_updated
    
    # Process Hotspot users
    hotspot_users, hotspot_updated = process_hotspot_users(api, router_name, existing_data)
    all_current_users.update(hotspot_users)
    updated = updated or hotspot_updated
    
    # Process deletions
    router_patterns = [
        router_name,
        f"PPP-{router_name}",
        f"HS-{router_name}"
    ]
    
    router_entries = {k: v for k, v in existing_data.items() 
                    if v.get('Parent Node') in router_patterns}
    
    for code in list(router_entries.keys()):
        if code not in all_current_users:
            del existing_data[code]
            logger.info(f"Removed user: {code} from router: {router_name}")
            updated = True
    
    return updated

def main_loop():
    """Main loop to process all routers."""
    routers = read_routers_csv()
    if not routers:
        logger.error("No routers found in the CSV file.")
        return
    
    # Check and update network.json
    # update_network_json(routers)

    while True:
        try:
            existing_data = read_shaped_devices_csv()
            updated = False

            for router in routers:
                logger.info(f"Processing router: {router['name']}")
                api = connect_to_router(router)
                
                if process_router(api, router['name'], existing_data):
                    updated = True
                
                # Close connection
                if api:
                    try:
                        api.disconnect()
                    except:
                        pass

            if updated:
                write_shaped_devices_csv(existing_data)
                logger.info("Updated ShapedDevices.csv with changes")
            else:
                logger.info("No changes detected")

            logger.info(f"Sleeping for {SCAN_INTERVAL} seconds before next check")
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(ERROR_RETRY_INTERVAL)

if __name__ == "__main__":
    main_loop()