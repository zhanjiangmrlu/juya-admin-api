"""MySQL 8.4 cold connections must support the server's default SHA-2 auth."""

from collections.abc import Callable

import pytest
from asyncmy.auth import sha2_rsa_encrypt as asyncmy_encrypt
from Crypto.Cipher import PKCS1_OAEP
from Crypto.PublicKey import RSA
from pymysql._auth import sha2_rsa_encrypt as pymysql_encrypt


@pytest.mark.parametrize("encrypt", [pymysql_encrypt, asyncmy_encrypt])
def test_mysql_drivers_support_rsa_password_exchange(
    encrypt: Callable[[bytes, bytes, bytes], bytes],
) -> None:
    # Failure to ship the RSA extra only appears after the server auth cache clears.
    # 功能:验证 MySQL 驱动支持 RSA 密码交换。
    # 参数:
    #     encrypt: 是否启用秘密加密的测试配置。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    key = RSA.generate(2048)
    password = b"database-auth-probe"
    challenge = b"0123456789abcdefghij"
    ciphertext = encrypt(password, challenge, key.public_key().export_key())

    scrambled = PKCS1_OAEP.new(key).decrypt(ciphertext)
    recovered = bytes(value ^ challenge[i % len(challenge)] for i, value in enumerate(scrambled))
    assert recovered == password + b"\0"
