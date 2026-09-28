import base64
import secrets

from Crypto.Cipher import AES

_PREFIX = b"v1:"


def encrypt_secret(value: str, key: bytes, *, nonce: bytes | None = None) -> bytes:
    _validate_key(key)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce or secrets.token_bytes(12))
    ciphertext, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
    payload = bytes(cipher.nonce) + tag + ciphertext
    return _PREFIX + base64.urlsafe_b64encode(payload)


def decrypt_secret(value: bytes, key: bytes | None, *, allow_plaintext: bool = False) -> str:
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
    if value is None:
        return None
    key = base64.urlsafe_b64decode(value.encode())
    _validate_key(key)
    return key


def _validate_key(key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("secret encryption key must decode to exactly 32 bytes")
