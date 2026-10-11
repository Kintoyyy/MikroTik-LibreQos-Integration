import re
import random
import string
import logging

from settings import (
    MIN_DL_RATE_PERCENTAGE, MIN_UL_RATE_PERCENTAGE,
    MAX_DL_RATE_PERCENTAGE, MAX_UL_RATE_PERCENTAGE,
    DEFAULT_DL_BANDWIDTH, DEFAULT_UL_BANDWIDTH, ID_LENGTH,
)

logger = logging.getLogger(__name__)


class RateResolver:
    MIN_DL_RATE_PERCENTAGE = MIN_DL_RATE_PERCENTAGE
    MIN_UL_RATE_PERCENTAGE = MIN_UL_RATE_PERCENTAGE
    MAX_DL_RATE_PERCENTAGE = MAX_DL_RATE_PERCENTAGE
    MAX_UL_RATE_PERCENTAGE = MAX_UL_RATE_PERCENTAGE
    DEFAULT_DL_BANDWIDTH   = DEFAULT_DL_BANDWIDTH
    DEFAULT_UL_BANDWIDTH   = DEFAULT_UL_BANDWIDTH
    ID_LENGTH              = ID_LENGTH

    RE_LIST_RATE = re.compile(r'(\d+(?:\.\d+)?[kmgKMG])/(\d+(?:\.\d+)?[kmgKMG])')
    RE_BANDWIDTH = re.compile(r'(\d+(?:\.\d+)?)([kmgKMG])?')

    @staticmethod
    def generate_short_id(length=None):
        if length is None:
            length = RateResolver.ID_LENGTH
        return ''.join(random.choices(string.digits + string.ascii_uppercase, k=length))

    @staticmethod
    def convert_to_mbps(value_str):
        try:
            if not value_str or value_str == '0':
                return '0'
            m = RateResolver.RE_BANDWIDTH.match(value_str.strip())
            if not m:
                return '0'
            number = float(m.group(1))
            unit = (m.group(2) or '').lower()
            if unit == 'k':
                return str(round(number / 1000, 2))
            elif unit == 'g':
                return str(round(number * 1000, 2))
            return str(round(number, 2))
        except Exception as e:
            logger.warning(f"Could not convert bandwidth '{value_str}': {e}")
            return '0'

    @staticmethod
    def is_valid_rate(rx, tx):
        """Return True only if both values parse as positive numbers."""
        try:
            return float(rx) > 0 and float(tx) > 0
        except (ValueError, TypeError):
            return False

    @staticmethod
    def parse_rate(rate_str):
        """
        Parse a rate string like '50M/50M'. The entire string must match.
        Returns (rx_mbps, tx_mbps) if valid, else None.
        """
        if not rate_str:
            return None
        m = RateResolver.RE_LIST_RATE.fullmatch(rate_str.strip())
        if m:
            rx = RateResolver.convert_to_mbps(m.group(1))
            tx = RateResolver.convert_to_mbps(m.group(2))
            if RateResolver.is_valid_rate(rx, tx):
                return rx, tx
        return None

    # LibreQoS rejects ShapedDevices.csv rows with min < 1 Mbps or max < 2 Mbps.
    MIN_RATE_FLOOR = 1
    MAX_RATE_FLOOR = 2

    @staticmethod
    def calculate_min_rates(max_rx, max_tx):
        """Min (guaranteed) rate = max x MIN_*_RATE_PERCENTAGE, at least 1 Mbps and never above max."""
        try:
            rx, tx = float(max_rx), float(max_tx)
        except (ValueError, TypeError):
            rx, tx = 0, 0

        def _min(max_rate, pct):
            floor = RateResolver.MIN_RATE_FLOOR
            return min(max(int(max_rate * pct), floor), max(int(max_rate), floor))

        return (
            _min(rx, RateResolver.MIN_DL_RATE_PERCENTAGE),
            _min(tx, RateResolver.MIN_UL_RATE_PERCENTAGE),
        )

    @staticmethod
    def calculate_max_rates(rx, tx):
        try:
            rx_f, tx_f = float(rx), float(tx)
        except (ValueError, TypeError):
            rx_f, tx_f = 0, 0
        return (
            max(int(rx_f * RateResolver.MAX_DL_RATE_PERCENTAGE), RateResolver.MAX_RATE_FLOOR),
            max(int(tx_f * RateResolver.MAX_UL_RATE_PERCENTAGE), RateResolver.MAX_RATE_FLOOR),
        )

    @staticmethod
    def build_comment(source, rate_str, rate_failed, label=''):
        """
        Format: 'source | rate' or 'source | label | rate'.
        No timestamp: the comment is part of change detection, and a value
        that differs every scan would make every device look updated.
        The last_seen column already records when a device was seen.
        """
        rate_label = '[default]' if rate_failed else (rate_str or '[default]')
        if label:
            return f"{source} | {label} | {rate_label}"
        return f"{source} | {rate_label}"

    @staticmethod
    def extract_first_rate(text):
        """
        Return the first X/X rate token found in a free-form string, or '' if none.
        Handles comma/space-separated lists and MikroTik rate-limit strings.
        """
        if not text:
            return ''
        for token in re.split(r'[\s,]+', text.strip()):
            token = token.strip()
            if token and RateResolver.parse_rate(token):
                return token
        return ''

    @staticmethod
    def resolve_rate_with_fallback(list_name, comment_str, rate_limit_str, default_dl, default_ul):
        """
        Rate resolution fallback chain:
          1. address-list name  (e.g. '50M/50M')
          2. comment field
          3. rate-limit / rate field
          4. config default
        Returns (rx_max, tx_max, rx_min, tx_min, rate_failed, rate_source, rate_str_used).
        """
        for source, raw in [
            ('address_list', list_name),
            ('comment',      comment_str),
            ('rate_limit',   rate_limit_str),
        ]:
            token = RateResolver.extract_first_rate(raw)
            if token:
                rx_max, tx_max, rx_min, tx_min, failed = RateResolver.resolve_rates(
                    token, default_dl, default_ul
                )
                return rx_max, tx_max, rx_min, tx_min, failed, source, token

        rx_max, tx_max, rx_min, tx_min, _ = RateResolver.resolve_rates('', default_dl, default_ul)
        return rx_max, tx_max, rx_min, tx_min, True, 'default', ''

    @staticmethod
    def resolve_rates(rate_str, default_dl, default_ul):
        """
        Try to parse rate_str. If valid, apply MAX/MIN multipliers.
        Falls back to config defaults.
        Returns (rx_max, tx_max, rx_min, tx_min, rate_failed).
        rate_failed is True when rate_str could not be parsed and defaults were used.
        """
        rate = RateResolver.parse_rate(rate_str)
        rate_failed = rate is None
        rx_raw, tx_raw = rate if rate else (str(default_dl), str(default_ul))
        rx_max, tx_max = RateResolver.calculate_max_rates(rx_raw, tx_raw)
        rx_min, tx_min = RateResolver.calculate_min_rates(rx_max, tx_max)
        return rx_max, tx_max, rx_min, tx_min, rate_failed
