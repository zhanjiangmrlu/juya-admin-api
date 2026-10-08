import base64

import pytest

from juya_admin_api.infrastructure.security.encrypted_secret import (
    decode_key,
    decrypt_secret,
    encrypt_secret,
)


def test_totp_secret_uses_authenticated_encryption() -> None:
    # 功能:验证 TOTP 秘密使用认证加密。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    key = b"k" * 32
    encrypted = encrypt_secret("JBSWY3DPEHPK3PXP", key, nonce=b"n" * 12)

    assert encrypted != b"JBSWY3DPEHPK3PXP"
    assert decrypt_secret(encrypted, key) == "JBSWY3DPEHPK3PXP"
    assert decode_key(base64.urlsafe_b64encode(key).decode()) == key

    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(ValueError):
        decrypt_secret(tampered, key)
