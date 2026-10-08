import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import alibabacloud_oss_v2 as oss
import pytest

from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider, CredentialsProvider
from juya_admin_api.shared.errors import AppError


class FakeClient:
    def __init__(self) -> None:
        # 功能:初始化 FakeClient 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeClient 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.deleted: list[str] = []

    def head_object(self, request: Any) -> Any:
        # 功能:返回测试对象的 MIME、大小和完整性等元数据。
        # 参数:
        #     self: 当前 FakeClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
        # 返回:Any,由本用例预设的数据或所组装的测试资源构成。
        return SimpleNamespace(
            content_length=123,
            content_type="image/png",
            metadata={
                "sha256": "a" * 64,
                "decodable": "true",
                "security_status": "PASSED",
            },
        )

    def presign(self, request: Any, **kwargs: Any) -> Any:
        # 功能:模拟 SDK 生成签名 URL 并记录请求与选项。
        # 参数:
        #     self: 当前 FakeClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:含预设 URL 字段的签名结果对象。
        assert kwargs["expires"].total_seconds() == 60
        return SimpleNamespace(url="https://signed.example/object")

    def delete_object(self, request: Any) -> None:
        # 功能:模拟删除指定对象并记录清理操作。
        # 参数:
        #     self: 当前 FakeClient 测试替身实例,保存本用例的预设状态或调用记录。
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.deleted.append(request.key)


@pytest.mark.asyncio
async def test_aliyun_adapter_builds_v4_policy_and_maps_object_operations() -> None:
    # 功能:验证阿里云适配器生成 V4 策略并映射对象操作。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    credentials = oss.credentials.StaticCredentialsProvider("access-key", "access-secret")
    provider = AliyunOssProvider(
        "cn-hangzhou",
        "private-bucket",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        credentials_provider=cast(CredentialsProvider, cast(Any, credentials)),
    )
    fake_client = FakeClient()
    provider._client = cast(Any, fake_client)

    policy = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 600)
    decoded = json.loads(base64.b64decode(policy.fields["policy"]))
    metadata = await provider.head_object("uploads/images/admin-1/a.png")
    signed = await provider.sign_get_url("uploads/images/admin-1/a.png", 60)
    await provider.delete_object("uploads/images/admin-1/a.png")

    assert policy.upload_url == "https://private-bucket.oss-cn-hangzhou.aliyuncs.com"
    assert ["starts-with", "$key", "uploads/images/admin-1/"] in decoded["conditions"]
    assert ["content-length-range", 1, 1024] in decoded["conditions"]
    assert metadata.sha256 == "a" * 64
    assert metadata.metadata["security_status"] == "PASSED"
    assert signed == "https://signed.example/object"
    assert fake_client.deleted == ["uploads/images/admin-1/a.png"]


@pytest.mark.asyncio
async def test_sts_token_is_bound_in_policy_and_mime_is_restricted() -> None:
    # 功能:验证 STS 令牌绑定上传策略且 MIME 受限。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    credentials = oss.credentials.StaticCredentialsProvider("test-id", "test-secret", "test-token")
    provider = AliyunOssProvider(
        "cn-shenzhen", "juya-test", credentials_provider=cast(Any, credentials)
    )
    policy = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    conditions = json.loads(base64.b64decode(policy.fields["policy"]))["conditions"]
    assert {"x-oss-security-token": "test-token"} in conditions
    assert [
        "in",
        "$Content-Type",
        ["image/jpeg", "image/png", "image/webp", "image/bmp"],
    ] in conditions
    assert policy.fields["Content-Type"] == "image/jpeg"
    assert "test-secret" not in repr(policy)
    assert "test-token" not in repr(policy)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix", ["", "other/", "uploads/images/../", "uploads/images/a\\b/", "https://bad/"]
)
async def test_upload_policy_rejects_keys_outside_authorized_prefixes(prefix: str) -> None:
    # 功能:验证上传策略拒绝授权前缀外的对象键。
    # 参数:
    #     prefix: 参数化测试使用的对象路径前缀。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )
    with pytest.raises(AppError) as error:
        await provider.create_upload_policy(prefix, 1024, 300)
    assert error.value.code == "OSS_OBJECT_KEY_INVALID"


@pytest.mark.asyncio
async def test_sts_expiry_caps_upload_policy_and_expired_credentials_fail_safely() -> None:
    # 功能:验证 STS 到期限制上传策略且过期凭证安全失败。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    now = datetime(2026, 9, 30, tzinfo=UTC)
    credentials = oss.credentials.StaticCredentialsProvider("id", "secret", "token")
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, credentials),
        credentials_expires_at=now + timedelta(seconds=90),
        clock=lambda: now,
    )
    policy = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    assert policy.expires_in <= 60
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    expired = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, credentials),
        credentials_expires_at=now,
        clock=lambda: now,
    )
    with pytest.raises(AppError) as error:
        await expired.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    assert error.value.code == "OSS_CREDENTIALS_EXPIRED"


@pytest.mark.asyncio
async def test_provider_failure_does_not_leak_signed_url_or_sdk_credentials() -> None:
    # 功能:验证服务失败不会泄露签名 URL 或 SDK 凭证。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )

    class FailingClient:
        def head_object(self, request: Any) -> None:
            # 功能:返回测试对象的 MIME、大小和完整性等元数据。
            # 参数:
            #     self: 当前 FailingClient 测试替身实例,保存本用例的预设状态或调用记录。
            #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
            # 返回:不产生正常结果;抛出当前用例预设的错误。
            raise RuntimeError("secret https://example.test/a?x-oss-signature=private")

    provider._client = cast(Any, FailingClient())
    with pytest.raises(AppError) as error:
        await provider.head_object("uploads/images/admin-1/a.png")
    assert error.value.code == "OSS_OPERATION_FAILED"
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_environment_credentials_are_refreshed_between_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证每次生成策略可刷新环境凭证。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "old-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "old-secret")
    provider = AliyunOssProvider("cn-shenzhen", "juya-test")
    first = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "new-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "new-secret")
    second = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    assert first.fields["x-oss-credential"].startswith("old-id/")
    assert second.fields["x-oss-credential"].startswith("new-id/")


@pytest.mark.asyncio
async def test_direct_upload_cannot_claim_a_trusted_security_verdict() -> None:
    # 功能:验证直接上传不能自行声明可信安全结论。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )
    policy = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    conditions = json.loads(base64.b64decode(policy.fields["policy"]))["conditions"]
    assert {"x-oss-meta-security_status": "PENDING"} in conditions
    assert {"x-oss-meta-decodable": "false"} in conditions
    assert policy.fields["x-oss-meta-security_status"] == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(4, b"abcd"), (3, None)])
async def test_bounded_read_uses_sdk_stream_iterator_and_always_closes(
    limit: int, expected: bytes | None
) -> None:
    # 功能:验证限量读取使用 SDK 流迭代器且始终关闭流。
    # 参数:
    #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
    #     expected: 参数化测试提供的预期结果,用于与实际返回值比较。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )

    class Body:
        closed = False

        def read(self) -> bytes:
            # 功能:拒绝 SDK 无界 read 调用,确保实现使用限量流读取。
            # 参数:
            #     self: 当前 Body 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            pytest.fail("SDK read() is unbounded and does not accept a length")

        def iter_bytes(self, *, chunk_size: int):
            # 功能:按测试设定块大小迭代返回媒体字节。
            # 参数:
            #     self: 当前 Body 测试替身实例,保存本用例的预设状态或调用记录。
            #     chunk_size: 模拟 SDK 读取流每次返回的最大字节数。
            # 返回:按块产生预设媒体字节的迭代器。
            assert chunk_size == 64 * 1024
            yield b"ab"
            yield b"cd"
            if limit < 4:
                pytest.fail("must stop consuming stream immediately at limit")

        def close(self) -> None:
            # 功能:模拟资源关闭;记录或更新关闭状态供清理断言。
            # 参数:
            #     self: 当前 Body 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.closed = True

    body = Body()
    # 匿名函数: 模拟 OSS SDK 读取响应, 提供可迭代字节流以检查有界读取。
    # 参数:
    #     request: SDK 读取请求, 当前替身保留接口但返回预设字节流。
    # 返回: 包含 content_length 和 body 流对象的模拟响应。
    provider._client = cast(
        Any,
        SimpleNamespace(get_object=lambda request: SimpleNamespace(content_length=0, body=body)),
    )
    if expected is None:
        with pytest.raises(AppError) as error:
            await provider.read_bytes("uploads/images/admin-1/a.png", limit)
        assert error.value.code == "MEDIA_SIZE_INVALID"
    else:
        assert await provider.read_bytes("uploads/images/admin-1/a.png", limit) == expected
    assert body.closed
