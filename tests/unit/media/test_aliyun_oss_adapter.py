import base64
import json
from types import SimpleNamespace
from typing import Any, cast

import alibabacloud_oss_v2 as oss
import pytest

from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider, CredentialsProvider


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
