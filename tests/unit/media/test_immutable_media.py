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
        # 功能:初始化 MutableUploadOss 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 MutableUploadOss 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        super().__init__(png_bytes())
        self.fixed: dict[str, bytes] = {}

    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        # 功能:模拟封存媒体并返回固定命名空间内的测试对象键。
        # 参数:
        #     self: 当前 MutableUploadOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     data: 待读取或封存的媒体原始字节。
        #     asset_type: 媒体资源类别,例如 images 或 audio。
        #     content_type: 媒体 MIME 类型,供上传及封存元数据检查。
        # 返回:固定命名空间内的测试对象键。
        key = f"sealed/media/{asset_type}/fixture.png"
        self.fixed[key] = data
        return key

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        # 功能:读取预设测试媒体字节,供服务端解码及限量读取检查。
        # 参数:
        #     self: 当前 MutableUploadOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     max_bytes: 允许读取或上传的字节数上限。
        # 返回:预设测试媒体的原始字节。
        return self.fixed.get(object_key, self.data)

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能:模拟对象下载签名并保留有效期供断言。
        # 参数:
        #     self: 当前 MutableUploadOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:预设资源签名 URL 字符串。
        return f"https://fixture/{object_key}"


@pytest.mark.asyncio
async def test_legacy_asset_is_fixed_only_when_the_published_hash_still_matches() -> None:
    # 功能:验证旧资源仅在已发布哈希仍一致时被固定。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证未确认上传对象不能获得教学下载签名。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service = MediaService(MutableUploadOss(), InMemoryMediaRepository())
    with pytest.raises(AppError) as error:
        await service.sign_media("uploads/images/admin-1/unconfirmed.png", None, NOW)
    assert error.value.code == "MEDIA_ASSET_UNAVAILABLE"


@pytest.mark.asyncio
async def test_confirmed_reference_and_download_keep_checked_bytes_after_upload_changes() -> None:
    # 功能:验证上传变化后确认引用及下载仍使用检查过的字节。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证浏览器上传不能写固定命名空间且要求禁止覆盖。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证封存检查版本控制状态并固定实际版本。
    # 参数:
    #     state: 参数化测试预设的 OSS 版本控制状态。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from types import SimpleNamespace

    provider = AliyunOssProvider(
        "cn-shenzhen",
        "fixture",
        credentials_provider=oss.credentials.StaticCredentialsProvider("id", "secret"),
    )
    captured: list[Any] = []

    class Client:
        def get_bucket_versioning(self, request: Any) -> Any:
            # 功能:返回预设 OSS 桶版本控制状态。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
            # 返回:Any,由本用例预设的数据或所组装的测试资源构成。
            return SimpleNamespace(version_status=state)

        def put_object(self, request: Any) -> Any:
            # 功能:记录 SDK 对象写入并返回预设版本标识。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
            # 返回:Any,由本用例预设的数据或所组装的测试资源构成。
            captured.append(request)
            return SimpleNamespace(version_id="stable-version" if state == "Enabled" else None)

        def presign(self, request: Any, **kwargs: Any) -> Any:
            # 功能:模拟 SDK 生成签名 URL 并记录请求与选项。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
            #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
            # 返回:预设签名结果对象,包含 URL 及有效期。
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
