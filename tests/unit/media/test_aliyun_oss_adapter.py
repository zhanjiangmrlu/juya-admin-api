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
        self.deleted: list[str] = []

    def head_object(self, request: Any) -> Any:
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
        assert kwargs["expires"].total_seconds() == 60
        return SimpleNamespace(url="https://signed.example/object")

    def delete_object(self, request: Any) -> None:
        self.deleted.append(request.key)


@pytest.mark.asyncio
async def test_aliyun_adapter_builds_v4_policy_and_maps_object_operations() -> None:
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
    now = datetime(2026, 9, 30, tzinfo=UTC)
    credentials = oss.credentials.StaticCredentialsProvider("id", "secret", "token")
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, credentials),
        credentials_expires_at=now + timedelta(seconds=90),
        clock=lambda: now,
    )
    policy = await provider.create_upload_policy("uploads/images/admin-1/", 1024, 300)
    assert policy.expires_in <= 60
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
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )

    class FailingClient:
        def head_object(self, request: Any) -> None:
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
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )

    class Body:
        closed = False

        def read(self) -> bytes:
            pytest.fail("SDK read() is unbounded and does not accept a length")

        def iter_bytes(self, *, chunk_size: int):
            assert chunk_size == 64 * 1024
            yield b"ab"
            yield b"cd"
            if limit < 4:
                pytest.fail("must stop consuming stream immediately at limit")

        def close(self) -> None:
            self.closed = True

    body = Body()
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
