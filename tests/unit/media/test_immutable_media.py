import base64
import json
from datetime import UTC, datetime
from typing import Any

import alibabacloud_oss_v2 as oss
import pytest
from test_v13_media import BytesOss, Security, png_bytes

from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaAsset, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 1, tzinfo=UTC)


class MutableUploadOss(BytesOss):
    def __init__(self) -> None:
        super().__init__(png_bytes())
        self.fixed: dict[str, bytes] = {}

    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        key = f"sealed/media/{asset_type}/fixture.png"
        self.fixed[key] = data
        return key

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        return self.fixed.get(object_key, self.data)

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        return f"https://fixture/{object_key}"


@pytest.mark.asyncio
async def test_legacy_asset_is_fixed_only_when_the_published_hash_still_matches() -> None:
    import hashlib

    provider = MutableUploadOss()
    repo = InMemoryMediaRepository()
    asset = MediaAsset(
        "legacy",
        "uploads/images/admin-1/old.png",
        "images",
        "image/png",
        len(png_bytes()),
        hashlib.sha256(png_bytes()).hexdigest(),
        "CONFIRMED",
        "PASSED",
        "admin-1",
        NOW,
    )
    await repo.save(asset)
    service = MediaService(provider, repo)
    signed = await service.sign_media(asset.object_key, None, NOW)
    assert "sealed/media/" in signed.url
    assert (await repo.get(asset.id)).object_key.startswith("sealed/media/")
    provider.data = png_bytes(80, 80)
    assert await service.read_asset_bytes(await repo.get(asset.id)) == png_bytes()
    stale = MediaAsset(
        "stale",
        "uploads/images/admin-1/stale.png",
        "images",
        "image/png",
        1,
        "1" * 64,
        "CONFIRMED",
        "PASSED",
        "admin-1",
        NOW,
    )
    await repo.save(stale)
    with pytest.raises(AppError) as error:
        await service.sign_media(stale.object_key, None, NOW)
    assert error.value.code == "MEDIA_ASSET_CHANGED"


@pytest.mark.asyncio
async def test_unconfirmed_uploaded_object_cannot_receive_a_teaching_download_signature() -> None:
    service = MediaService(MutableUploadOss(), InMemoryMediaRepository())
    with pytest.raises(AppError) as error:
        await service.sign_media("uploads/images/admin-1/unconfirmed.png", None, NOW)
    assert error.value.code == "MEDIA_ASSET_UNAVAILABLE"


@pytest.mark.asyncio
async def test_confirmed_reference_and_download_keep_checked_bytes_after_upload_changes() -> None:
    provider = MutableUploadOss()
    service = MediaService(provider, InMemoryMediaRepository(), security=Security("PASSED"))
    asset = await service.confirm_upload("images", "admin-1", "uploads/images/admin-1/a.png", NOW)
    provider.data = png_bytes(80, 80)
    assert asset.object_key.startswith("sealed/media/")
    assert await service.read_asset_bytes(asset) == png_bytes()
    signed = await service.sign_media(asset.object_key, None, NOW)
    assert "sealed/media/" in signed.url


@pytest.mark.asyncio
async def test_browser_policy_cannot_target_fixed_namespace_and_requires_no_overwrite() -> None:
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "fixture",
        credentials_provider=oss.credentials.StaticCredentialsProvider("id", "secret"),
    )
    with pytest.raises(AppError):
        await provider.create_upload_policy("sealed/media/images/", 1024, 300)
    policy = await provider.create_upload_policy("uploads/images/admin/nonce/", 1024, 300)
    conditions = json.loads(base64.b64decode(policy.fields["policy"]))["conditions"]
    assert {"x-oss-forbid-overwrite": "true"} in conditions
    assert policy.fields["x-oss-forbid-overwrite"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["Enabled", "Suspended", ""])
async def test_freeze_checks_version_state_and_pins_real_version(state: str) -> None:
    from types import SimpleNamespace

    provider = AliyunOssProvider(
        "cn-shenzhen",
        "fixture",
        credentials_provider=oss.credentials.StaticCredentialsProvider("id", "secret"),
    )
    captured: list[Any] = []

    class Client:
        def get_bucket_versioning(self, request: Any) -> Any:
            return SimpleNamespace(version_status=state)

        def put_object(self, request: Any) -> Any:
            captured.append(request)
            return SimpleNamespace(version_id="stable-version" if state == "Enabled" else None)

        def presign(self, request: Any, **kwargs: Any) -> Any:
            captured.append(request)
            return SimpleNamespace(url="https://fixed")

    provider._client = Client()
    if state == "Suspended":
        with pytest.raises(AppError) as error:
            await provider.freeze_bytes(png_bytes(), "images", "image/png")
        assert error.value.code == "OSS_VERSIONING_UNSAFE"
        assert captured == []
    else:
        key = await provider.freeze_bytes(png_bytes(), "images", "image/png")
        assert captured[0].forbid_overwrite == "true"
        await provider.sign_get_url(key, 60)
        assert captured[-1].version_id == ("stable-version" if state else None)
