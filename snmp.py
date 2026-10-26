"""
Minimal SNMP v2c client in pure Python: GET, and GETBULK walks of a subtree.

Enough to read router health, session counts and interface counters without
an extra package or net-snmp. Values come back as Python ints (INTEGER,
Counter32/64, Gauge32, TimeTicks), str (OCTET STRING, IpAddress, OID) or
None (noSuchObject, noSuchInstance, endOfMibView, NULL).
"""
import os
import socket
import struct

VERSION_2C = 1
GET, GETNEXT, RESPONSE, GETBULK = 0xA0, 0xA1, 0xA2, 0xA5
_INTEGER, _OCTETS, _NULL, _OID, _SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
_IPADDRESS, _COUNTER32, _GAUGE32, _TIMETICKS, _OPAQUE, _COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x44, 0x46
_NO_SUCH_OBJECT, _NO_SUCH_INSTANCE, _END_OF_MIB = 0x80, 0x81, 0x82
GET_CHUNK = 30            # OIDs per GET request (keeps responses well under one packet)


class SnmpError(Exception):
    pass


# Typed values, for agents (tests, the preview) that must send the right ASN.1 type
class Counter32(int):
    pass


class Gauge32(int):
    pass


class TimeTicks(int):
    pass


class Counter64(int):
    pass


class _Missing:
    def __init__(self, tag, name):
        self.tag, self.name = tag, name

    def __repr__(self):
        return self.name


NO_SUCH_OBJECT = _Missing(_NO_SUCH_OBJECT, "noSuchObject")
NO_SUCH_INSTANCE = _Missing(_NO_SUCH_INSTANCE, "noSuchInstance")
END_OF_MIB_VIEW = _Missing(_END_OF_MIB, "endOfMibView")


def oid_tuple(oid):
    return tuple(int(p) for p in str(oid).strip(".").split(".") if p != "")


def oid_str(oid):
    return ".".join(map(str, oid))


def in_subtree(oid, base):
    o, b = oid_tuple(oid), oid_tuple(base)
    return o[:len(b)] == b and len(o) > len(b)


# ── BER ─────────────────────────────────────────────────────────────────────

def _length(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag, payload):
    return bytes([tag]) + _length(len(payload)) + payload


def _signed(v):
    return v.to_bytes(max(1, (v + (v < 0)).bit_length() // 8 + 1), "big", signed=True)


def _unsigned(v):
    body = v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")
    return b"\x00" + body if body[0] & 0x80 else body


def _base128(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _encode_oid(oid):
    parts = oid_tuple(oid)
    if len(parts) < 2:
        raise SnmpError(f"bad OID {oid!r}")
    return _tlv(_OID, bytes([parts[0] * 40 + parts[1]]) + b"".join(_base128(p) for p in parts[2:]))


def encode_value(v):
    if v is None:
        return _tlv(_NULL, b"")
    if isinstance(v, _Missing):
        return _tlv(v.tag, b"")
    for cls, tag in ((Counter64, _COUNTER64), (Counter32, _COUNTER32), (Gauge32, _GAUGE32), (TimeTicks, _TIMETICKS)):
        if isinstance(v, cls):
            return _tlv(tag, _unsigned(int(v)))
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return _tlv(_INTEGER, _signed(v))
    if isinstance(v, str):
        v = v.encode()
    return _tlv(_OCTETS, bytes(v))


def _decode(data, pos):
    """(tag, payload bytes, position after) of the TLV at pos."""
    try:
        tag = data[pos]
        n = data[pos + 1]
        pos += 2
        if n & 0x80:
            size = n & 0x7F
            n = int.from_bytes(data[pos:pos + size], "big")
            pos += size
        if pos + n > len(data):
            raise IndexError
        return tag, data[pos:pos + n], pos + n
    except IndexError:
        raise SnmpError("truncated SNMP message")


def _decode_oid(payload):
    if not payload:
        return ""
    first = payload[0]
    parts = [first // 40, first % 40] if first < 80 else [2, first - 80]
    n = 0
    for b in payload[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return oid_str(parts)


def decode_value(tag, payload):
    if tag == _INTEGER:
        return int.from_bytes(payload, "big", signed=True) if payload else 0
    if tag in (_COUNTER32, _GAUGE32, _TIMETICKS, _COUNTER64):
        return int.from_bytes(payload, "big")
    if tag == _OCTETS or tag == _OPAQUE:
        return payload.decode("utf-8", "replace")
    if tag == _IPADDRESS:
        return ".".join(map(str, payload))
    if tag == _OID:
        return _decode_oid(payload)
    return None                    # NULL, noSuchObject, noSuchInstance, endOfMibView


def _sequence(data):
    """The TLVs inside a constructed payload, as (tag, payload)."""
    out, pos = [], 0
    while pos < len(data):
        tag, payload, pos = _decode(data, pos)
        out.append((tag, payload))
    return out


def encode_message(community, pdu, request_id, varbinds, error_status=0, error_index=0, version=VERSION_2C):
    """varbinds: [(oid, value)]. For GETBULK, error_status/error_index carry non-repeaters/max-repetitions."""
    vbs = b"".join(_tlv(_SEQUENCE, _encode_oid(o) + encode_value(v)) for o, v in varbinds)
    body = (_tlv(_INTEGER, _signed(request_id)) + _tlv(_INTEGER, _signed(error_status))
            + _tlv(_INTEGER, _signed(error_index)) + _tlv(_SEQUENCE, vbs))
    community = community.encode() if isinstance(community, str) else community
    return _tlv(_SEQUENCE, _tlv(_INTEGER, _signed(version)) + _tlv(_OCTETS, community) + _tlv(pdu, body))


def decode_message(data):
    """(version, community, pdu tag, request id, error status, error index, [(oid, (tag, payload))])."""
    tag, message, _ = _decode(data, 0)
    if tag != _SEQUENCE:
        raise SnmpError("not an SNMP message")
    parts = _sequence(message)
    if len(parts) != 3:
        raise SnmpError("malformed SNMP message")
    version = decode_value(*parts[0])
    community = parts[1][1]
    pdu, body = parts[2]
    fields = _sequence(body)
    if len(fields) != 4:
        raise SnmpError("malformed SNMP PDU")
    request_id, status, index = (decode_value(*f) for f in fields[:3])
    varbinds = []
    for _, vb in _sequence(fields[3][1]):
        (otag, opayload), value = _sequence(vb)
        varbinds.append((_decode_oid(opayload), value))
    return version, community, pdu, request_id, status, index, varbinds


# ── Client ──────────────────────────────────────────────────────────────────

class Client:
    """
    SNMP v2c requests to one agent over UDP:

        with Client("192.0.2.1", "public") as c:
            c.get(["1.3.6.1.2.1.1.5.0"])          # {"1.3.6.1.2.1.1.5.0": "router"}
            c.walk("1.3.6.1.2.1.31.1.1.1.1")      # [(oid, value), ...]
    """
    def __init__(self, host, community="public", port=161, timeout=2.0, retries=1):
        self.addr = (host, int(port))
        self.community = community
        self.timeout, self.retries = float(timeout), int(retries)
        self.sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, pdu, oids, non_repeaters=0, max_repetitions=0):
        request_id = struct.unpack(">I", os.urandom(4))[0] & 0x7FFFFFFF
        message = encode_message(self.community, pdu, request_id, [(o, None) for o in oids],
                                 non_repeaters, max_repetitions)
        for _ in range(self.retries + 1):
            try:
                self.sock.sendto(message, self.addr)
                while True:                          # skip late replies to earlier requests
                    data, _ = self.sock.recvfrom(65535)
                    try:
                        _, _, rpdu, rid, status, index, varbinds = decode_message(data)
                    except SnmpError:
                        continue
                    if rpdu == RESPONSE and rid == request_id:
                        break
            except socket.timeout:
                continue
            except OSError as e:
                raise SnmpError(f"{self.addr[0]}:{self.addr[1]}: {e}")
            if status:
                names = {1: "tooBig", 2: "noSuchName", 3: "badValue", 5: "genErr"}
                raise SnmpError(f"agent error {names.get(status, status)} at varbind {index}")
            return [(oid, decode_value(*value)) for oid, value in varbinds], [value[0] for _, value in varbinds]
        raise SnmpError(f"no answer from {self.addr[0]}:{self.addr[1]} (timeout)")

    def get(self, oids):
        """{oid: value} for each OID (None where the agent has no such object)."""
        oids = [oid_str(oid_tuple(o)) for o in oids]
        out = {}
        for i in range(0, len(oids), GET_CHUNK):
            chunk = oids[i:i + GET_CHUNK]
            values, _ = self._request(GET, chunk)
            for asked, (_, value) in zip(chunk, values):
                out[asked] = value
        return out

    def walk(self, base, max_repetitions=50):
        """[(oid, value)] of every object under base, in order."""
        base = oid_str(oid_tuple(base))
        out, current = [], base
        while True:
            values, tags = self._request(GETBULK, [current], 0, max_repetitions)
            if not values:
                return out
            for (oid, value), tag in zip(values, tags):
                if tag == _END_OF_MIB or not in_subtree(oid, base):
                    return out
                if oid_tuple(oid) <= oid_tuple(current):
                    raise SnmpError(f"agent returned {oid} after {current} (not increasing)")
                out.append((oid, value))
                current = oid
