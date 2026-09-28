import secrets
from datetime import datetime

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD32[value & 31]
        value >>= 5
    return "".join(chars)


def new_ulid(now: datetime) -> str:
    timestamp_ms = int(now.timestamp() * 1000)
    return _encode_base32(timestamp_ms, 10) + _encode_base32(secrets.randbits(80), 16)
