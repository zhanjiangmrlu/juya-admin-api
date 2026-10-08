import secrets
from datetime import datetime

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    # 功能: 按固定字符数编码整数为 Crockford Base32 字符串.
    # 参数:
    #     value: 要编码的非负整数,包含时间戳或 ULID 随机部分.
    #     length: Base32 编码输出的字符数量.
    # 返回: 指定长度的 Crockford Base32 编码字符串.
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD32[value & 31]
        value >>= 5
    return "".join(chars)


def new_ulid(now: datetime) -> str:
    # 功能: 以毫秒时间和随机部分生成可排序的 ULID 标识.
    # 参数:
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 由毫秒时间和随机数编码的 26 字符 ULID.
    timestamp_ms = int(now.timestamp() * 1000)
    return _encode_base32(timestamp_ms, 10) + _encode_base32(secrets.randbits(80), 16)
