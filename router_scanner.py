import logging
import time

import routeros_api

from rate_resolver import RateResolver

logger = logging.getLogger(__name__)


class RouterScanner:
    def __init__(self, db):
        # db: DeviceDatabase — scanner writes discovered devices through it
        self.db = db

    # ── Router connection ───────────────────────────────────────────────────

    @staticmethod
    def connect(router, retries=3):
        """
        Open a RouterOS API connection with retry logic.
        Returns the API object on success, None after all retries are exhausted.
        """
        for attempt in range(retries):
            try:
                api = routeros_api.RouterOsApiPool(
                    router['address'],
                    username=router['username'],
                    password=router['password'],
                    port=router['port'],
                    plaintext_login=True,
                ).get_api()
                logger.info(
                    f"Connected to {router['name']} ({router['address']}) "
                    f"[attempt {attempt + 1}]"
                )
                return api
            except Exception as e:
                logger.warning(
                    f"Connection error to {router['name']} "
                    f"[attempt {attempt + 1}/{retries}]: {e}"
                )
                if attempt == retries - 1:
                    logger.error(f"Failed to connect to {router['name']}")
                    return None
                time.sleep(5)

    @staticmethod
    def get_resource_data(api, resource_path):
        """Fetch a RouterOS resource, returning an empty list on error."""
        try:
            return api.get_resource(resource_path).get()
        except Exception as e:
            logger.error(f"Failed to fetch {resource_path}: {e}")
            return []

    # ── Public scan entry point ─────────────────────────────────────────────

    def scan_router(self, router, scan_time) -> bool:
        """
        Connect to one router, collect all device sources, and persist to the DB.
        Returns True if any device data changed.
        """
        api = self.connect(router)
        if api is None:
            logger.warning(f"Skipping {router['name']} — connection failed.")
            return False

        try:
            addr_list_entries = self.get_resource_data(api, '/ip/firewall/address-list')

            # Build IP→list_name map used by PPPoE and hotspot rate lookups
            ip_to_list = {
                e['address']: e.get('list', '')
                for e in addr_list_entries
                if e.get('address') and e.get('disabled', 'false') != 'true'
            }

            changed = False
            changed |= self._process_pppoe_users(api, router, ip_to_list, scan_time)
            changed |= self._process_hotspot_users(api, router, ip_to_list, scan_time)
            changed |= self._process_dhcp_leases(api, router, ip_to_list, scan_time)
            changed |= self._process_address_list(router, addr_list_entries, scan_time)

            self.db.conn.commit()
            return changed

        except Exception as e:
            logger.error(f"Error processing router {router['name']}: {e}")
            self.db.conn.rollback()
            return False

    # ── Private processors ──────────────────────────────────────────────────

    def _process_pppoe_users(self, api, router, ip_to_list, scan_time) -> bool:
        """
        Active PPPoE sessions: get NAME, CALLER-ID (MAC), ADDRESS.
        Rate is looked up by IP in the address list. Falls back to config default.
        """
        if not router.get('pppoe', {}).get('enabled', False):
            logger.info(f"PPPoE disabled for {router['name']}")
            return False

        router_name = router['name']
        default_dl  = router.get('pppoe', {}).get('default_download_limit', 10)
        default_ul  = router.get('pppoe', {}).get('default_upload_limit', 10)
        changed     = False

        sessions = self.get_resource_data(api, '/ppp/active')
        logger.info(f"PPPoE: {len(sessions)} active sessions on {router_name}")

        for session in sessions:
            name      = session.get('name', '')
            address   = session.get('address', '')
            caller_id = session.get('caller-id', '')

            if not address:
                continue

            code           = f"PPP-{name}"
            mac            = caller_id.upper()
            list_name      = ip_to_list.get(address, '')
            comment_field  = session.get('comment', '')
            rate_limit_str = session.get('rate', '')

            rx_max, tx_max, rx_min, tx_min, rate_failed, rate_src, rate_used = \
                RateResolver.resolve_rate_with_fallback(
                    list_name, comment_field, rate_limit_str, default_dl, default_ul
                )
            logger.debug(
                f"PPPoE {code}: IP={address} src={rate_src} "
                f"rate='{rate_used}' → {rx_max}/{tx_max} Mbps"
            )

            comment = RateResolver.build_comment('pppoe', rate_used or list_name, rate_failed, scan_time)

            if self.db.upsert_device(
                code, '', mac, address, comment, 'pppoe', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed

    def _process_hotspot_users(self, api, router, ip_to_list, scan_time) -> bool:
        """
        Active hotspot sessions: get USER, MAC-ADDRESS, ADDRESS.
        Rate is looked up by IP in the address list. Falls back to config default.
        """
        if not router.get('hotspot', {}).get('enabled', False):
            logger.info(f"Hotspot disabled for {router['name']}")
            return False

        router_name = router['name']
        default_dl  = router.get('hotspot', {}).get('default_download_limit', 10)
        default_ul  = router.get('hotspot', {}).get('default_upload_limit', 10)
        changed     = False

        users = self.get_resource_data(api, '/ip/hotspot/active')
        logger.info(f"Hotspot: {len(users)} active users on {router_name}")

        for user in users:
            username = user.get('user', '')
            mac      = user.get('mac-address', '').upper()
            address  = user.get('address', '')

            if (not username and not mac) or not address:
                continue

            mac_clean = mac.replace(':', '')
            code = f"HS-{mac_clean}" if mac_clean else f"HS-{username}"

            list_name      = ip_to_list.get(address, '')
            comment_field  = user.get('comment', '')
            rate_limit_str = user.get('rate', '')

            rx_max, tx_max, rx_min, tx_min, rate_failed, rate_src, rate_used = \
                RateResolver.resolve_rate_with_fallback(
                    list_name, comment_field, rate_limit_str, default_dl, default_ul
                )
            logger.debug(
                f"Hotspot {code}: IP={address} src={rate_src} "
                f"rate='{rate_used}' → {rx_max}/{tx_max} Mbps"
            )

            comment = RateResolver.build_comment('hotspot', rate_used or list_name, rate_failed, scan_time)

            if self.db.upsert_device(
                code, '', mac, address, comment, 'hotspot', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed

    def _process_dhcp_leases(self, api, router, ip_to_list, scan_time) -> bool:
        """
        DHCP leases: rate from address-list field on the lease, comment, or default.
        """
        if not router.get('dhcp', {}).get('enabled', False):
            logger.info(f"DHCP disabled for {router['name']}")
            return False

        router_name = router['name']
        default_dl  = router.get('dhcp', {}).get('default_download_limit', 1000)
        default_ul  = router.get('dhcp', {}).get('default_upload_limit', 1000)
        changed     = False

        leases = self.get_resource_data(api, '/ip/dhcp-server/lease')
        logger.info(f"DHCP: {len(leases)} leases on {router_name}")

        for lease in leases:
            mac = lease.get('mac-address', '').upper()
            if not mac:
                continue

            address         = lease.get('address', '')
            hostname        = lease.get('host-name', '')
            addr_list_field = lease.get('address-list', lease.get('address-lists', ''))
            comment_field   = lease.get('comment', '')
            rate_limit_str  = lease.get('rate-limit', '')

            rx_max, tx_max, rx_min, tx_min, rate_failed, rate_src, rate_used = \
                RateResolver.resolve_rate_with_fallback(
                    addr_list_field, comment_field, rate_limit_str, default_dl, default_ul
                )
            logger.debug(
                f"DHCP {mac}: IP={address} src={rate_src} "
                f"rate='{rate_used}' → {rx_max}/{tx_max} Mbps"
            )

            mac_clean = mac.replace(':', '')
            code    = f"DHCP-{hostname}" if hostname else f"DHCP-{mac_clean}"
            comment = RateResolver.build_comment(
                'dhcp', rate_used or addr_list_field, rate_failed, scan_time
            )

            if self.db.upsert_device(
                code, '', mac, address, comment, 'dhcp', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed

    def _process_address_list(self, router, addr_list_entries, scan_time) -> bool:
        """
        Standalone address list entries: rate comes directly from the list name.
        Falls back to config default.
        """
        router_name = router['name']
        default_dl  = router.get('address_list', {}).get('default_download_limit', 100)
        default_ul  = router.get('address_list', {}).get('default_upload_limit', 100)
        changed     = False

        entries = [
            e for e in addr_list_entries
            if e.get('disabled', 'false') != 'true'
            and e.get('address')
            and RateResolver.parse_rate(e.get('list', ''))
        ]
        logger.info(f"Address list: {len(entries)} entries for {router_name}")

        for entry in entries:
            address   = entry.get('address', '')
            list_name = entry.get('list', '')
            comment   = entry.get('comment', '')
            code      = comment if comment else f"ADDR-{address}"

            rx_max, tx_max, rx_min, tx_min, rate_failed = \
                RateResolver.resolve_rates(list_name, default_dl, default_ul)
            logger.debug(f"AddrList {code}: list='{list_name}' → {rx_max}/{tx_max} Mbps")

            entry_comment = RateResolver.build_comment(
                'address_list', list_name, rate_failed, scan_time
            )

            if self.db.upsert_device(
                code, '', '', address, entry_comment, 'address_list', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed
