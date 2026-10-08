import base64
import secrets

from Crypto.Cipher import AES

_PREFIX = b"v1:"


def encrypt_secret(value: str, key: bytes, *, nonce: bytes | None = None) -> bytes:
    # 功能: 使用 AES-GCM 加密文本并附带版本和随机数前缀.
    # 参数:
    #     value: 待加密的密钥明文.
    #     key: AES-GCM 加密密钥,必须恰好为 32 字节.
    #     nonce: AES-GCM 加密随机数,需为 12 字节;None 时自动生成,同一密钥下不可复用.
    # 返回: 带 v1 前缀的密文,封装 12 字节随机数,认证标签及加密文本.
    _validate_key(key)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce or secrets.token_bytes(12))
    ciphertext, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
    payload = bytes(cipher.nonce) + tag + ciphertext
    return _PREFIX + base64.urlsafe_b64encode(payload)


def decrypt_secret(value: bytes, key: bytes | None, *, allow_plaintext: bool = False) -> str:
    # 功能: 解密版本化密钥内容,并按配置兼容历史明文.
    # 参数:
    #     value: 带版本前缀的密文字节,或允许兼容的历史明文字节.
    #     key: AES-GCM 加密密钥,必须恰好为 32 字节;解密入口允许传入 None 以触发缺失校验.
    #     allow_plaintext: 是否允许解码未带加密版本前缀的历史明文密钥.
    # 返回: 解密并通过认证标签校验的密钥明文,或配置允许的历史明文.
    if not value.startswith(_PREFIX):
        if allow_plaintext:
            return value.decode("utf-8")
        raise ValueError("encrypted secret format is required")
    if key is None:
        raise ValueError("secret encryption key is required")
    _validate_key(key)
    payload = base64.urlsafe_b64decode(value[len(_PREFIX) :])
    if len(payload) < 29:
        raise ValueError("encrypted secret payload is invalid")
    nonce, tag, ciphertext = payload[:12], payload[12:28], payload[28:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")


def decode_key(value: str | None) -> bytes | None:
    # 功能: 解码环境配置中的 Base64 加密密钥.
    # 参数:
    #     value: Base64 编码的加密密钥配置;None 表示未设置.
    # 返回: 解码后的密钥字节;未配置时为 None.
    if value is None:
        return None
    key = base64.urlsafe_b64decode(value.encode())
    _validate_key(key)
    return key


def _validate_key(key: bytes) -> None:
    # 功能: 校验 AES-GCM 密钥字节长度.
    # 参数:
    #     key: AES-GCM 加密密钥,必须恰好为 32 字节.
    # 返回: 无返回值;正常完成表示本次操作成功.
    if len(key) != 32:
        raise ValueError("secret encryption key must decode to exactly 32 bytes")
