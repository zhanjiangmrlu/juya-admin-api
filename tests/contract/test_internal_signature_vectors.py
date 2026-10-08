from juya_admin_api.infrastructure.security.service_hmac import sign_request


def test_miniapp_request_signature_contract() -> None:
    # 功能:验证跨服务请求签名满足小程序接口契约。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert (
        sign_request(
            method="POST",
            path_with_query="/internal/v1/users/search?limit=20",
            timestamp=1_790_553_600,
            nonce="nonce-123",
            body=b'{"query":"JUYA-1"}',
            secret=b"test-secret",
        )
        == "e63fa6ef766cf0a563147cbc60a7edc53130c8837c4397d7d7f34c1a3170c231"
    )
