import contextlib
import logging
import time

import routeros_api

from rate_resolver import RateResolver
from settings import CONNECT_RETRIES, CONNECT_RETRY_DELAY

logger = logging.getLogger(__name__)

# Optional per-router sources: (config key, RouterOS path)
_SOURCES = (
    ('pppoe',   '/ppp/active'),
    ('hotspot', '/ip/hotspot/active'),
    ('dhcp',    '/ip/dhcp-server/lease'),
)


class RouterScanner:
    # Last connection error per router name, for job/error reporting
    connect_errors = {}

    def __init__(self, db):
        # db: DeviceDatabase — scanner writes discovered devices through it
        self.db = db
        # Why the last scan_router() call failed ('' when it succeeded)
        self.last_error = ''

    # ── Router connection ───────────────────────────────────────────────────

    @staticmethod
    @contextlib.contextmanager
    def open_api(router, retries=None):
        """
        Open a RouterOS API connection with retry logic, and always close it.
        Yields the API object, or None after all retries are exhausted.

            with RouterScanner.open_api(router) as api:
                ...
        """
        retries = retries or CONNECT_RETRIES
        pool = api = None
        for attempt in range(retries):
            try:
                pool = routeros_api.RouterOsApiPool(
                    router['address'],
                    username=router.get('username', ''),
                    password=router.get('password', ''),
                    port=int(router.get('port', 8728) or 8728),
                    plaintext_login=True,
                )
                api = pool.get_api()
                RouterScanner.connect_errors.pop(router['name'], None)
                logger.info(
                    f"Connected to {router['name']} ({router['address']}) "
                    f"[attempt {attempt + 1}]"
                )
                break
            except Exception as e:
                RouterScanner.connect_errors[router['name']] = str(e) or type(e).__name__
                logger.warning(
                    f"Connection error to {router['name']} "
                    f"[attempt {attempt + 1}/{retries}]: {e}"
                )
                RouterScanner._disconnect(pool)
                pool = api = None
                if attempt == retries - 1:
                    logger.error(f"Failed to connect to {router['name']}")
                else:
                    time.sleep(CONNECT_RETRY_DELAY)
        try:
            yield api
        finally:
            RouterScanner._disconnect(pool)

    @staticmethod
    def _disconnect(pool):
        if pool is not None:
            try:
                pool.disconnect()
            except Exception:
                pass

    # ── Public scan entry point ─────────────────────────────────────────────

    def scan_router(self, router, scan_time):
        """
        Connect to one router, collect all device sources, and persist to the DB.

        Returns (ok, changed). ok is False when the router could not be read
        completely; nothing is written for it then, and its existing devices
        are kept instead of being treated as gone.
        """
        name = router['name']
        self.last_error = ''
        with self.open_api(router) as api:
            if api is None:
                logger.warning(f"Skipping {name} — connection failed; keeping its devices.")
                self.last_error = f"connection failed: {RouterScanner.connect_errors.get(name, '')}"
                return False, False
            try:
                data = self._fetch_all(api, router)
            except Exception as e:
                logger.error(f"Failed to read from {name}: {e} — keeping its devices.")
                self.last_error = f"read failed: {e}"
                return False, False

        try:
            addr_list_entries = data['address_list']

            # Build IP→list_name map used by PPPoE and hotspot rate lookups
            ip_to_list = {
                e['address']: e.get('list', '')
                for e in addr_list_entries
                if e.get('address') and e.get('disabled', 'false') != 'true'
            }

            changed = False
            changed |= self._process_pppoe_users(router, data['pppoe'], ip_to_list, scan_time)
            changed |= self._process_hotspot_users(router, data['hotspot'], ip_to_list, scan_time)
            changed |= self._process_dhcp_leases(router, data['dhcp'], scan_time)
            changed |= self._process_address_list(router, addr_list_entries, scan_time)

            self.db.conn.commit()
            return True, changed

        except Exception as e:
            logger.error(f"Error processing router {name}: {e}")
            self.last_error = f"processing failed: {e}"
            self.db.conn.rollback()
            return False, False

    @staticmethod
    def _fetch_all(api, router):
        """
        Read everything needed from the router before touching the DB, so the
        DB write lock is never held during network I/O. Raises on any API
        error: a partial read must not look like "all those devices are gone".
        Disabled sources are returned as None.
        """
        data = {'address_list': api.get_resource('/ip/firewall/address-list').get()}
        for key, path in _SOURCES:
            if router.get(key, {}).get('enabled', False):
                try:
                    data[key] = api.get_resource(path).get()
                except Exception as e:
                    raise RuntimeError(f"{path}: {e}") from e
            else:
                data[key] = None
        return data

    # ── Private processors ──────────────────────────────────────────────────

    def _process_pppoe_users(self, router, sessions, ip_to_list, scan_time) -> bool:
        """
        Active PPPoE sessions: get NAME, CALLER-ID (MAC), ADDRESS.
        Rate is looked up by IP in the address list. Falls back to config default.
        """
        if sessions is None:
            logger.info(f"PPPoE disabled for {router['name']}")
            return False

        router_name = router['name']
        default_dl  = router.get('pppoe', {}).get('default_download_limit', 10)
        default_ul  = router.get('pppoe', {}).get('default_upload_limit', 10)
        changed     = False

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

            comment = RateResolver.build_comment('pppoe', rate_used or list_name, rate_failed)

            if self.db.upsert_device(
                code, mac, address, comment, 'pppoe', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed

    def _process_hotspot_users(self, router, users, ip_to_list, scan_time) -> bool:
        """
        Active hotspot sessions: get USER, MAC-ADDRESS, ADDRESS.
        Rate is looked up by IP in the address list. Falls back to config default.
        """
        if users is None:
            logger.info(f"Hotspot disabled for {router['name']}")
            return False

        router_name = router['name']
        default_dl  = router.get('hotspot', {}).get('default_download_limit', 10)
        default_ul  = router.get('hotspot', {}).get('default_upload_limit', 10)
        changed     = False

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

            comment = RateResolver.build_comment('hotspot', rate_used or list_name, rate_failed)

            if self.db.upsert_device(
                code, mac, address, comment, 'hotspot', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed

    def _process_dhcp_leases(self, router, leases, scan_time) -> bool:
        """
        DHCP leases: rate from address-list field on the lease, comment, or default.
        Devices are keyed by MAC — host-names are client-chosen and not unique.
        """
        if leases is None:
            logger.info(f"DHCP disabled for {router['name']}")
            return False

        router_name = router['name']
        default_dl  = router.get('dhcp', {}).get('default_download_limit', 1000)
        default_ul  = router.get('dhcp', {}).get('default_upload_limit', 1000)
        changed     = False

        logger.info(f"DHCP: {len(leases)} leases on {router_name}")

        for lease in leases:
            mac     = lease.get('mac-address', '').upper()
            address = lease.get('address', '')
            if not mac or not address or lease.get('disabled', 'false') == 'true':
                continue

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

            code    = f"DHCP-{mac.replace(':', '')}"
            comment = RateResolver.build_comment(
                'dhcp', rate_used or addr_list_field, rate_failed, label=hostname
            )

            if self.db.upsert_device(
                code, mac, address, comment, 'dhcp', router_name,
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

        used_codes = set()
        for entry in entries:
            address   = entry.get('address', '')
            list_name = entry.get('list', '')
            comment   = entry.get('comment', '')
            # The comment names the device, but two entries can share one.
            code      = comment if comment and comment not in used_codes else f"ADDR-{address}"
            used_codes.add(code)

            rx_max, tx_max, rx_min, tx_min, rate_failed = \
                RateResolver.resolve_rates(list_name, default_dl, default_ul)
            logger.debug(f"AddrList {code}: list='{list_name}' → {rx_max}/{tx_max} Mbps")

            entry_comment = RateResolver.build_comment('address_list', list_name, rate_failed)

            if self.db.upsert_device(
                code, '', address, entry_comment, 'address_list', router_name,
                rx_max, tx_max, rx_min, tx_min, scan_time
            ):
                changed = True

        return changed
