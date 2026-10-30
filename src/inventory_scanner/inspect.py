"""Local decoding of self-contained CS2 inspect payloads.

Format reference: github.com/csfloat/cs-inspect-serializer (see THIRD_PARTY_NOTICES.md).
Legacy S…A…D… / M…A…D… links are retained for the user, never guessed or remotely resolved.
"""
from __future__ import annotations

import math
import re
import struct
import zlib
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class InspectMetadata:
    item_id: str | None
    float_value: float | None
    pattern_seed: int | None


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated inspect field")
        value = data[offset]
        offset += 1
        if shift == 63 and value > 1:
            raise ValueError("Inspect integer overflow")
        result |= (value & 127) << shift
        if not value & 128:
            return result, offset
    raise ValueError("Inspect integer overflow")


def _fields(payload: bytes) -> dict[int, int]:
    result: dict[int, int] = {}
    offset = 0
    while offset < len(payload):
        key, offset = _varint(payload, offset)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("Invalid inspect field")
        if wire == 0:
            result[number], offset = _varint(payload, offset)
        elif wire == 1:
            offset += 8
        elif wire == 2:
            length, offset = _varint(payload, offset)
            offset += length
        elif wire == 5:
            offset += 4
        else:
            raise ValueError("Unsupported inspect wire type")
        if offset > len(payload):
            raise ValueError("Truncated inspect payload")
    return result


def decode_inspect(link: str) -> InspectMetadata | None:
    """Return unknown for legacy/malformed links; never turn unknown into zero wear."""
    if not isinstance(link, str) or len(link) > 32768:
        return None
    decoded = unquote(link.strip())
    match = re.fullmatch(
        r"(?:steam://(?:run|rungame)/730/(?:76561202255233023/)?/?)?"
        r"\+?csgo_econ_action_preview\s+([0-9a-f]+)", decoded, re.I,
    )
    if not match or len(match[1]) % 2:
        return None
    try:
        data = bytes.fromhex(match[1])
        if len(data) < 6:
            return None
        masked = data[0] != 0
        if masked:
            data = bytes(value ^ data[0] for value in data)
        payload = data[1:-4]
        if not masked:
            crc = zlib.crc32(b"\0" + payload)
            # JS CRC32.buf is signed; the final low 32 bits are identical here.
            expected = ((crc & 0xFFFF) ^ (len(payload) * crc)) & 0xFFFFFFFF
            if int.from_bytes(data[-4:], "big") != expected:
                return None
        fields = _fields(payload)
        if masked and not {2, 3, 4, 13, 14} <= fields.keys():
            return None
        if not {2, 3, 4, 8} & fields.keys():
            return None
        wear = None
        if 7 in fields:
            if fields[7] > 0xFFFFFFFF:
                return None
            wear = struct.unpack("<f", struct.pack("<I", fields[7]))[0]
            if not math.isfinite(wear) or not 0 <= wear <= 1:
                return None
        seed = fields.get(8)
        if seed is not None and not 0 <= seed <= 1000:
            return None
        return InspectMetadata(str(fields[2]) if fields.get(2) else None, wear, seed)
    except (ValueError, OverflowError, struct.error):
        return None

