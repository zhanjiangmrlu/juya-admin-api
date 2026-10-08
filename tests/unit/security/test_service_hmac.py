from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from starlette.requests import Request

from juya_admin_api.infrastructure.security.service_hmac import (
    INTERNAL_NONCE_TTL_SECONDS,
    ServicePrincipal,
    sign_request,
    verify_request_signature,
)
from juya_admin_api.shared.errors import AppError


class MemoryNonceStore:
    def __init__(self) -> None:
        # 功能:初始化 MemoryNonceStore 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 MemoryNonceStore 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.seen: set[tuple[str, str]] = set()
        self.ttls: list[int] = []

    async def use_once(self, service_name: str, nonce: str, ttl_seconds: int) -> bool:
        # 功能:记录服务随机值并返回是否首次出现,模拟防重放缓存。
        # 参数:
        #     self: 当前 MemoryNonceStore 测试替身实例,保存本用例的预设状态或调用记录。
        #     service_name: 已认证内部服务的名称,用于隔离随机值命名空间。
        #     nonce: HMAC 请求随机值,用于防止签名请求重放。
        #     ttl_seconds: 防重放随机值的缓存存活时间,单位为秒。
        # 返回:随机值首次使用时为 True,重放时为 False。
        key = (service_name, nonce)
        self.ttls.append(ttl_seconds)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def make_request(
    *,
    body: bytes,
    signature: str,
    timestamp: int = 1_790_553_600,
    nonce: str = "nonce-123",
) -> Request:
    # 功能:构造携带请求体、时间戳、随机值及 HMAC 签名的 ASGI 请求。
    # 参数:
    #     body: 用于签名校验的原始 HTTP 请求体字节。
    #     signature: 传给测试请求的 HMAC 签名字符串。
    #     timestamp: 签名请求的 Unix 时间戳,单位为秒。
    #     nonce: HMAC 请求随机值,用于防止签名请求重放。
    # 返回:构造的 ASGI 请求。
    delivered = False

    async def receive() -> dict[str, object]:
        # 功能:提供 ASGI 请求体接收事件。
        # 参数:无。
        # 返回:包含 HTTP 请求体字节的 ASGI 事件字典。
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [
        (b"x-juya-service", b"miniapp-api"),
        (b"x-juya-timestamp", str(timestamp).encode()),
        (b"x-juya-nonce", nonce.encode()),
        (b"x-juya-signature", signature.encode()),
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/internal/v1/users/search",
            "raw_path": b"/internal/v1/users/search",
            "query_string": b"limit=20",
            "headers": headers,
            "client": ("10.0.0.2", 12345),
            "server": ("admin.internal", 443),
        },
        receive,
    )


def test_sign_request_matches_cross_service_golden_vector() -> None:
    # 功能:验证请求签名符合跨服务固定测试向量。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    signature = sign_request(
        method="POST",
        path_with_query="/internal/v1/users/search?limit=20",
        timestamp=1_790_553_600,
        nonce="nonce-123",
        body=b'{"query":"JUYA-1"}',
        secret=b"test-secret",
    )

    assert signature == "e63fa6ef766cf0a563147cbc60a7edc53130c8837c4397d7d7f34c1a3170c231"


@pytest.mark.asyncio
async def test_verify_request_returns_authenticated_service() -> None:
    # 功能:验证签名校验返回已认证服务主体。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    body = b'{"query":"JUYA-1"}'
    signature = sign_request(
        "POST",
        "/internal/v1/users/search?limit=20",
        1_790_553_600,
        "nonce-123",
        body,
        b"test-secret",
    )
    store = MemoryNonceStore()

    principal = await verify_request_signature(
        make_request(body=body, signature=signature),
        SecretStr("test-secret"),
        store,
        datetime.fromtimestamp(1_790_553_600, UTC),
    )

    assert principal == ServicePrincipal(service_name="miniapp-api")
    assert store.ttls == [INTERNAL_NONCE_TTL_SECONDS]


@pytest.mark.asyncio
async def test_verify_request_rejects_tampered_body() -> None:
    # 功能:验证签名校验拒绝被篡改请求体。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    signature = sign_request(
        "POST",
        "/internal/v1/users/search?limit=20",
        1_790_553_600,
        "nonce-123",
        b'{"query":"JUYA-1"}',
        b"test-secret",
    )

    with pytest.raises(AppError, match="签名无效") as error:
        await verify_request_signature(
            make_request(body=b'{"query":"JUYA-2"}', signature=signature),
            SecretStr("test-secret"),
            MemoryNonceStore(),
            datetime.fromtimestamp(1_790_553_600, UTC),
        )

    assert error.value.code == "INVALID_INTERNAL_SIGNATURE"


@pytest.mark.asyncio
async def test_verify_request_rejects_expired_timestamp() -> None:
    # 功能:验证签名校验拒绝过期时间戳。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    body = b"{}"
    signature = sign_request(
        "POST",
        "/internal/v1/users/search?limit=20",
        1_790_553_600,
        "nonce-123",
        body,
        b"test-secret",
    )

    with pytest.raises(AppError, match="时间戳已过期") as error:
        await verify_request_signature(
            make_request(body=body, signature=signature),
            SecretStr("test-secret"),
            MemoryNonceStore(),
            datetime.fromtimestamp(1_790_553_600, UTC) + timedelta(seconds=301),
        )

    assert error.value.code == "INTERNAL_SIGNATURE_EXPIRED"


@pytest.mark.asyncio
async def test_verify_request_rejects_nonce_replay() -> None:
    # 功能:验证签名校验拒绝重复随机值。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    body = b"{}"
    signature = sign_request(
        "POST",
        "/internal/v1/users/search?limit=20",
        1_790_553_600,
        "nonce-123",
        body,
        b"test-secret",
    )
    store = MemoryNonceStore()
    now = datetime.fromtimestamp(1_790_553_600, UTC)

    await verify_request_signature(
        make_request(body=body, signature=signature), SecretStr("test-secret"), store, now
    )
    with pytest.raises(AppError, match="请求已处理") as error:
        await verify_request_signature(
            make_request(body=body, signature=signature), SecretStr("test-secret"), store, now
        )

    assert error.value.code == "INTERNAL_REQUEST_REPLAYED"
